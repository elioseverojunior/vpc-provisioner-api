#!/usr/bin/env python3

import base64
import ipaddress
import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
import jwt
from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWKClient
from mangum import Mangum
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("vpc_provisioner_api")

# ------------------------------------------------------------------------------
# 1. AWS API Gateway / Lambda Path Handling
# ------------------------------------------------------------------------------
# If deploying to AWS, API Gateway often introduces a stage name prefix (e.g., /Prod or /dev).
# Setting 'root_path' prevents Swagger UI from looking for /openapi.json at the absolute root.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
STAGE = os.environ.get("STAGE", "")
ROOT_PATH = f"/{STAGE}" if STAGE else ""

app = FastAPI(
    title="Serverless VPC Provisioner API",
    description="""
    A serverless API designed to provision AWS VPC environments on-demand.

    ### Features
    * Automatically creates VPCs and associated subnets.
    * Tags resources dynamically.
    * Persists infrastructure metadata inside DynamoDB.
    * Fully compatible with AWS Lambda, API Gateway, and LocalStack.
    """,
    version="1.0.0",
    root_path=ROOT_PATH,
    docs_url="/docs",  # Swagger UI endpoint
    redoc_url="/redoc"  # ReDoc alternative endpoint
)

# Do not instantiate Mangum at import time during local tests — Mangum may
# call asyncio.get_event_loop() which triggers a DeprecationWarning on some
# Python/asyncio combinations when there is no running event loop. Create the
# handler lazily only when running inside AWS Lambda.
handler = None
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_EXECUTION_ENV"):
    handler = Mangum(app)  # Adapts FastAPI to AWS Lambda

# ------------------------------------------------------------------------------
# 2. Infrastructure Initialization
# ------------------------------------------------------------------------------
IS_LOCAL = os.environ.get("LOCALSTACK_HOSTNAME") or os.environ.get("AWS_SAM_LOCAL")
if IS_LOCAL:
    if os.environ.get("LOCALSTACK_HOSTNAME"):
        message = f"STARTING WITH LOCALSTACK HOST: {IS_LOCAL}"
    if os.environ.get("AWS_SAM_LOCAL"):
        message = f"STARTING WITH AWS SAM LOCAL: {IS_LOCAL}"

    logger.info(message)

    ENDPOINT_URL = f"http://{os.environ.get('LOCALSTACK_HOSTNAME', 'localhost')}:4566"

    aws_access_key_id = "test"
    aws_secret_access_key = "test"

    ec2 = boto3.client(
        "ec2",
        endpoint_url=ENDPOINT_URL,
        region_name=AWS_REGION,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=ENDPOINT_URL,
        region_name=AWS_REGION,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
else:
    ec2 = boto3.client("ec2", region_name=AWS_REGION)
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "VpcProvisionerApiMetadata")
table = dynamodb.Table(TABLE_NAME)

# ------------------------------------------------------------------------------
# 3. Security & Swagger Authentication Integration
# ------------------------------------------------------------------------------
# HTTPBearer auto-populates the "Authorize" padlock UI component in Swagger.
# auto_error=False so that under IAM/SigV4 auth (where the Authorization header
# carries an AWS signature, not a Bearer token) the dependency doesn't reject
# the request before verify_token can decide based on AUTH_MODE.
security = HTTPBearer(scheme_name="Bearer Token", auto_error=False)

# Auth mode. "cognito" (default): the app validates Cognito JWTs / falls back to
# a presence check locally. "iam": the HTTP API Gateway authenticates callers
# via SigV4 (execute-api:Invoke), so the app trusts the gateway and skips the
# in-app token check. Set via the AUTH_MODE env var: template.yaml (default)
# sets "iam"; template-cognito.yaml leaves it as the "cognito" default.
AUTH_MODE = os.environ.get("AUTH_MODE", "cognito").lower()

# Cognito JWT validation config. When these are present (template-cognito.yaml
# injects them), verify_token performs real RS256 signature,
# issuer and expiry validation against the User Pool's JWKS — defense-in-depth
# behind the API Gateway authorizer, and protection against direct Lambda
# invocation. When absent (local dev / tests), it falls back to a minimal
# presence check so the API can run without Cognito.
COGNITO_REGION = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID")
COGNITO_APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID")
COGNITO_ISSUER = (
    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    if COGNITO_USER_POOL_ID
    else None
)

_jwks_client = None


def _get_jwks_client() -> PyJWKClient:
    """Lazily build a cached JWKS client for the configured User Pool."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{COGNITO_ISSUER}/.well-known/jwks.json")
    return _jwks_client


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """
    Validate the incoming HTTP Bearer token.

    With Cognito configured this verifies the JWT signature, issuer and expiry
    against the User Pool JWKS. Locally it falls back to a minimal presence
    check so the API can run without Cognito.

    Under AUTH_MODE=iam the API Gateway has already authenticated the caller via
    SigV4 (execute-api:Invoke), so the app performs no token check.
    """
    if AUTH_MODE == "iam":
        return None

    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    if COGNITO_ISSUER:
        try:
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=COGNITO_ISSUER,
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            logger.warning("JWT validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        # Cognito access tokens carry `client_id`; ID tokens carry `aud`.
        if COGNITO_APP_CLIENT_ID and COGNITO_APP_CLIENT_ID not in (
                claims.get("client_id"),
                claims.get("aud"),
        ):
            raise HTTPException(status_code=401, detail="Token audience mismatch")
        return claims

    # Local/dev fallback — no Cognito configured.
    if token == "invalid":
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return token


# ------------------------------------------------------------------------------
# 4. Pydantic Models (Request/Response Documentation)
# ------------------------------------------------------------------------------
def _validate_cidr(value: str) -> str:
    """Reject malformed CIDR blocks at the edge instead of after provisioning."""
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError("must be a valid CIDR block, e.g. 10.0.0.0/16")
    return value


class SubnetConfig(BaseModel):
    cidr_block: str = Field(..., description="The IPv4 CIDR block for the subnet.",
                            json_schema_extra={"example": "10.0.1.0/24"})
    availability_zone: str = Field(..., description="The AWS Availability Zone.",
                                   json_schema_extra={"example": "us-east-1a"})

    _check_cidr = field_validator("cidr_block")(_validate_cidr)


class CreateVpcRequest(BaseModel):
    vpc_cidr: str = Field(..., description="The primary IPv4 CIDR block for the VPC.",
                          json_schema_extra={"example": "10.0.0.0/16"})
    subnets: List[SubnetConfig] = Field(..., description="List of subnets to deploy inside the newly created VPC.")

    _check_cidr = field_validator("vpc_cidr")(_validate_cidr)


# ------------------------------------------------------------------------------
# 5. API Router Endpoints
# ------------------------------------------------------------------------------

@app.post(
    "/vpc",
    status_code=201,
    dependencies=[Depends(verify_token)],
    summary="Provision a new VPC environment",
    description="Provisions an isolated AWS VPC, waits for availability, attaches an Internet Gateway, builds a custom route table, creates subnets, and saves tracking records to DynamoDB."
)
def create_vpc(request: CreateVpcRequest):
    created_subnets = []
    vpc_id = None
    igw_id = None
    route_table_id = None
    try:
        # 1. Create VPC
        vpc_response = ec2.create_vpc(CidrBlock=request.vpc_cidr)
        vpc_id = vpc_response["Vpc"]["VpcId"]

        # Wait until VPC is available
        ec2.get_waiter("vpc_exists").wait(VpcIds=[vpc_id])

        # Tag the VPC
        ec2.create_tags(Resources=[vpc_id], Tags=[{"Key": "Name", "Value": f"API-Created-{vpc_id}"}])

        # 2. Create Internet Gateway & attach to VPC
        igw_response = ec2.create_internet_gateway()
        igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
        ec2.create_tags(Resources=[igw_id], Tags=[{"Key": "Name", "Value": f"API-Created-{vpc_id}-igw"}])

        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        # 3. Create Custom Route Table & add route to internet gateway
        route_table_response = ec2.create_route_table(VpcId=vpc_id)
        route_table_id = route_table_response["RouteTable"]["RouteTableId"]
        ec2.create_tags(Resources=[route_table_id], Tags=[{"Key": "Name", "Value": f"API-Created-{vpc_id}-rt"}])

        ec2.create_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=igw_id
        )

        # 4. Create Subnets & associate them with Route Table
        for subnet_cfg in request.subnets:
            sub_resp = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=subnet_cfg.cidr_block,
                AvailabilityZone=subnet_cfg.availability_zone
            )
            subnet_id = sub_resp["Subnet"]["SubnetId"]

            # Associate route table to make it public
            assoc_resp = ec2.associate_route_table(SubnetId=subnet_id, RouteTableId=route_table_id)
            association_id = assoc_resp["AssociationId"]

            created_subnets.append({
                "SubnetId": subnet_id,
                "CidrBlock": sub_resp["Subnet"]["CidrBlock"],
                "AvailabilityZone": sub_resp["Subnet"]["AvailabilityZone"],
                "RouteTableAssociationId": association_id
            })

        # 5. Store Results in DynamoDB
        record = {
            "VpcId": vpc_id,
            "CidrBlock": request.vpc_cidr,
            "InternetGatewayId": igw_id,
            "RouteTableId": route_table_id,
            "Subnets": created_subnets,
            "Status": "CREATED"
        }
        table.put_item(Item=record)

        return {"message": "VPC, Subnets, Internet Gateway, and Route Table created successfully", "data": record}

    except Exception:
        logger.exception("VPC creation failed; starting rollback (vpc_id=%s)", vpc_id)
        rollback_errors = []

        # Disassociate Route Table first
        for subnet in created_subnets:
            assoc_id = subnet.get("RouteTableAssociationId")
            if assoc_id:
                try:
                    ec2.disassociate_route_table(AssociationId=assoc_id)
                except Exception as assoc_err:
                    rollback_errors.append(f"disassociate {assoc_id}: {assoc_err}")
                    logger.error("Rollback failed to disassociate %s: %s", assoc_id, assoc_err)

        # Clean up subnets
        for subnet in created_subnets:
            try:
                ec2.delete_subnet(SubnetId=subnet["SubnetId"])
            except Exception as sub_err:
                rollback_errors.append(f"delete subnet {subnet['SubnetId']}: {sub_err}")
                logger.error("Rollback failed to delete subnet %s: %s", subnet["SubnetId"], sub_err)

        # Clean up Route Table
        if route_table_id:
            try:
                ec2.delete_route_table(RouteTableId=route_table_id)
            except Exception as rt_err:
                rollback_errors.append(f"delete route table {route_table_id}: {rt_err}")
                logger.error("Rollback failed to delete Route Table %s: %s", route_table_id, rt_err)

        # Clean up Internet Gateway
        if igw_id:
            if vpc_id:
                try:
                    ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                except Exception as detach_err:
                    rollback_errors.append(f"detach IGW {igw_id}: {detach_err}")
                    logger.error("Rollback failed to detach IGW %s: %s", igw_id, detach_err)
            try:
                ec2.delete_internet_gateway(InternetGatewayId=igw_id)
            except Exception as igw_err:
                rollback_errors.append(f"delete IGW {igw_id}: {igw_err}")
                logger.error("Rollback failed to delete IGW %s: %s", igw_id, igw_err)

        # Clean up VPC
        if vpc_id:
            try:
                ec2.delete_vpc(VpcId=vpc_id)
            except Exception as vpc_err:
                rollback_errors.append(f"delete VPC {vpc_id}: {vpc_err}")
                logger.error("Rollback failed to delete VPC %s: %s", vpc_id, vpc_err)

        # If rollback could not fully clean up, persist a tracking record so the
        # orphaned resources stay visible (GET /vpcs) and deletable later instead
        # of leaking silently and accruing cost.
        if vpc_id and rollback_errors:
            try:
                table.put_item(Item={
                    "VpcId": vpc_id,
                    "CidrBlock": request.vpc_cidr,
                    "InternetGatewayId": igw_id,
                    "RouteTableId": route_table_id,
                    "Subnets": created_subnets,
                    "Status": "ROLLBACK_INCOMPLETE",
                    "RollbackErrors": rollback_errors,
                })
            except Exception as persist_err:
                logger.error("Failed to persist ROLLBACK_INCOMPLETE record for %s: %s", vpc_id, persist_err)

        raise HTTPException(status_code=500, detail="Creation failed. Rollback initiated.")


@app.delete(
    "/vpc/{vpc_id}",
    status_code=200,
    dependencies=[Depends(verify_token)],
    summary="Teardown and delete an existing VPC",
    description="Deletes all underlying tracked subnets, route tables, and internet gateways from AWS EC2 first, tears down the VPC container, and scrubs the tracking registry entry from DynamoDB."
)
def delete_vpc(vpc_id: str):
    try:
        # 1. Fetch metadata from DynamoDB to know what subnets to delete
        response = table.get_item(Key={"VpcId": vpc_id})
        if "Item" not in response:
            raise HTTPException(status_code=404, detail="VPC metadata not found in database")

        vpc_data = response["Item"]
        subnets = vpc_data.get("Subnets", [])
        igw_id = vpc_data.get("InternetGatewayId")
        route_table_id = vpc_data.get("RouteTableId")

        # 2. Disassociate Route Table first
        for subnet in subnets:
            assoc_id = subnet.get("RouteTableAssociationId")
            if assoc_id:
                try:
                    ec2.disassociate_route_table(AssociationId=assoc_id)
                except ec2.exceptions.ClientError as e:
                    logger.warning("Skipping disassociation for %s: %s", assoc_id, e)

        # 3. Delete Subnets
        for subnet in subnets:
            try:
                ec2.delete_subnet(SubnetId=subnet["SubnetId"])
            except ec2.exceptions.ClientError as e:
                logger.warning("Skipping subnet %s: %s", subnet["SubnetId"], e)

        # 4. Delete Route Table
        if route_table_id:
            try:
                ec2.delete_route_table(RouteTableId=route_table_id)
            except ec2.exceptions.ClientError as e:
                logger.warning("Skipping route table %s: %s", route_table_id, e)

        # 4. Detach and Delete Internet Gateway
        if igw_id:
            try:
                ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            except ec2.exceptions.ClientError as e:
                logger.warning("Skipping IGW detachment for %s: %s", igw_id, e)
            try:
                ec2.delete_internet_gateway(InternetGatewayId=igw_id)
            except ec2.exceptions.ClientError as e:
                logger.warning("Skipping IGW deletion for %s: %s", igw_id, e)

        # 5. Delete the VPC itself
        try:
            ec2.delete_vpc(VpcId=vpc_id)
        except ec2.exceptions.ClientError as e:
            raise HTTPException(status_code=400, detail=f"Failed to delete AWS VPC: {str(e)}")

        # 6. Remove the tracking metadata record from DynamoDB
        table.delete_item(Key={"VpcId": vpc_id})

        return {"message": f"VPC {vpc_id} and its associated network routing resources were successfully deleted."}

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.exception("Failed to delete VPC %s", vpc_id)
        raise HTTPException(status_code=500, detail="Failed to delete VPC.")


def _encode_token(last_key: dict) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque pagination cursor."""
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_token(token: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid next_token")


@app.get(
    "/vpcs",
    dependencies=[Depends(verify_token)],
    summary="List provisioned VPC records (paginated)",
    description="Returns a page of recorded entries from the DynamoDB inventory. Use 'next_token' to fetch subsequent pages; a null token indicates the last page."
)
def get_all_vpcs(
        limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return per page."),
        next_token: Optional[str] = Query(None, description="Pagination cursor returned by a previous call."),
):
    scan_kwargs: Dict[str, Any] = {"Limit": limit}
    if next_token:
        scan_kwargs["ExclusiveStartKey"] = _decode_token(next_token)

    response = table.scan(**scan_kwargs)
    items = response.get("Items", [])
    last_key = response.get("LastEvaluatedKey")

    return {
        "items": items,
        "count": len(items),
        "next_token": _encode_token(last_key) if last_key else None,
    }


@app.get(
    "/vpc/{vpc_id}",
    dependencies=[Depends(verify_token)],
    summary="Get single VPC specifications",
    description="Fetches properties, subnets configuration, and recorded status metadata for a single tracked VPC ID mapping."
)
def get_vpc(vpc_id: str):
    response = table.get_item(Key={"VpcId": vpc_id})
    if "Item" not in response:
        raise HTTPException(status_code=404, detail="VPC metadata not found")
    return response["Item"]
