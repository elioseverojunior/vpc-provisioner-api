# Serverless VPC Provisioner API

A Python-based, serverless API using FastAPI and AWS SAM to dynamically provision AWS Virtual Private Clouds (VPCs) with multiple subnets, keeping track of records within Amazon DynamoDB.

## Features
- **Serverless Architecture**: Utilizes AWS Lambda and API Gateway for scaling down to zero costs.
- **State Management**: Automatically logs all created infrastructure parameters to DynamoDB.
- **Protected Endpoints**: Secure token validation built into the API Gateway routing layer.

## Prerequisites
- [AWS CLI](https://aws.amazon.com/cli/) configured with deployment permissions.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html) installed.
- Python 3.12 (the AWS Lambda runtime target; the project pins `>=3.12,<3.13`).

## Deployment & Development Guide

### 1. Local Development (using LocalStack)

The repository provides a preconfigured local cloud stack using Docker Compose and LocalStack.

1. **Start LocalStack**:
   ```bash
   docker compose up -d
   ```
   *Note: This starts LocalStack and executes the initialization scripts inside `localstack/ready.d/` to bootstrap the DynamoDB tables and S3 buckets.*

2. **Install Python dependencies**:
   Ensure you have [uv](https://github.com/astral-sh/uv) or pip installed:
   ```bash
   uv sync
   ```

3. **Run the FastAPI server locally**:
   Set `AWS_SAM_LOCAL=1` to route Boto3 EC2 and DynamoDB calls to LocalStack:
   ```bash
   AWS_SAM_LOCAL=1 uv run uvicorn serverless.app:app --reload
   ```
   The API will now be listening on `http://127.0.0.1:8000`.

---

### 2. Running Automated Tests

A Python unit and integration test suite is located in the `tests/` directory.

Run the tests against the running LocalStack instance:
```bash
PYTHONPATH=. uv run pytest tests/
```

---

### 3. AWS Serverless Deployment (using AWS SAM)

To deploy the API to real AWS cloud infrastructure:

1. **Build the SAM template**:
   ```bash
   sam build
   ```

2. **Deploy to your AWS Account**:
   Ensure your AWS CLI credentials are configured, then run:
   ```bash
   sam deploy --guided
   ```
   During the interactive prompt, specify:
   - **Stack Name**: `vpc-provisioner-api`
   - **AWS Region**: e.g., `us-east-1`

   The default `template.yaml` uses **IAM (SigV4)** authorization and outputs an `InvokeApiPolicyArn` (a managed policy granting `execute-api:Invoke`). To use Cognito JWT auth instead, deploy `template-cognito.yaml` — see **3a. Authorization options** below.

---

### 3a. Authorization options

The API ships with two interchangeable authorization models — pick the one that matches your callers and deploy that template:

| | `template.yaml` (default) | `template-cognito.yaml` |
|---|---|---|
| **Mechanism** | IAM (SigV4) authorization | Cognito User Pool + HTTP API JWT authorizer |
| **Caller sends** | A SigV4-signed request (AWS credentials) | `Authorization: Bearer <JWT>` |
| **Best for** | AWS workforce / operators / CI, incl. **IAM Identity Center** permission sets | External apps / end-users (B2C) |
| **App auth** | App trusts the gateway (`AUTH_MODE=iam`) | In-app JWT validation (`AUTH_MODE=cognito`) |
| **"All authenticated users"** | Any IAM principal granted `execute-api:Invoke` | Any valid User Pool token |

> **Note:** IAM Identity Center is *not* a drop-in for a JWT authorizer — there is no API Gateway integration that validates Identity Center tokens directly. Instead, use IAM authorization: operators sign in to Identity Center, receive temporary IAM credentials via a permission set, and call the API with SigV4. The gateway authorizes against IAM.

**Deploying** — the `mise run deploy` task wraps build + deploy and selects the variant by flag:

```bash
mise run deploy                     # default IAM/SigV4 stack (template.yaml)
mise run deploy --cognito           # Cognito JWT variant (template-cognito.yaml)
mise run deploy --cognito --guided  # add --guided for the interactive flow
```

For the default IAM stack, the output `InvokeApiPolicyArn` is a managed policy granting `execute-api:Invoke`; attach it to the IAM roles or IAM Identity Center permission sets of the operators allowed to call the API.

**Calling the IAM-authorized API** requires SigV4-signed requests (a plain bearer token is rejected by the gateway):

```bash
# Using awscurl (pip install awscurl) — signs with your current AWS credentials
awscurl --service execute-api --region us-east-1 \
  -X GET "https://<api-id>.execute-api.us-east-1.amazonaws.com/vpcs"
```

In Postman, set the request **Authorization** type to **AWS Signature** and supply the credentials (or an assumed-role session) instead of a bearer token.

**Deploying the Cognito (JWT) variant instead:**

```bash
sam build --template template-cognito.yaml
sam deploy --guided --template template-cognito.yaml
```

This provisions a Cognito User Pool + Client and prints their IDs in the stack outputs. Onboard users and fetch a token as shown in **3b** below.

---

### 3b. Onboarding Cognito users (JWT mode)

When deployed with the **Cognito variant** (`template-cognito.yaml`), callers need a User Pool account and a token. The quickest path for demos/testing is the bundled `mise` task, which reads the stack outputs, creates the user, sets a permanent password, and prints a ready-to-use header:

```bash
EMAIL=user@example.com PASSWORD='S0me-Str0ng-Pass!' mise run onboard-user
# -> Authorization: Bearer <IdToken>
```

Overrides: `STACK_NAME` (default `vpc-provisioner-api`), `AWS_REGION` (default `us-east-1`). It requires AWS admin credentials (for the `admin-*` calls) and the Cognito stack to be deployed. The password appears in the process list, so use a throwaway value. (Not applicable to the default IAM stack, which has no User Pool.)

To do it by hand, the task wraps three calls: `aws cognito-idp admin-create-user` → `admin-set-user-password --permanent` → `initiate-auth --auth-flow USER_PASSWORD_AUTH` (use the returned **IdToken** — its `aud` matches the JWT authorizer). For self sign-up, SES email delivery, or corporate federation, see the [Amazon Cognito user pools docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-identity-pools.html).

---

### 4. Interactive API Documentation

When running locally, you can access interactive documentation pages:
- **Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

---

### 5. Testing with Postman

Import the JSON file inside the `postman/` directory into Postman to load pre-configured requests targeting:
- `POST /vpc` (creates VPC & subnets)
- `GET /vpcs` (paginated list of metadata)
- `GET /vpc/{vpc_id}` (get metadata by ID)
- `DELETE /vpc/{vpc_id}` (deletes subnets and parent VPC)

---

### 6. Listing VPCs (Pagination)

`GET /vpcs` is paginated. It accepts two optional query parameters:

| Param | Description |
|------|-------------|
| `limit` | Max records per page (1–1000, default `100`). |
| `next_token` | Opaque cursor from a previous response. Omit it for the first page. |

The response is an envelope rather than a bare array:

```json
{
  "items": [ { "VpcId": "vpc-...", "CidrBlock": "10.0.0.0/16", "Status": "CREATED" } ],
  "count": 1,
  "next_token": "eyJWcGNJZCI6ICJ2cGMtLi4uIn0="
}
```

When `next_token` is non-null, pass it back to fetch the next page; a `null` token indicates the last page:

```bash
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:8000/vpcs?limit=50"
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:8000/vpcs?limit=50&next_token=eyJWcGNJZCI6..."
```
