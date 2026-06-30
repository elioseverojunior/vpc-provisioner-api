import os
from fastapi.testclient import TestClient

# Set environment variable to route Boto3 to LocalStack before importing the app
os.environ["AWS_SAM_LOCAL"] = "1"
os.environ["DYNAMODB_TABLE"] = "VpcProvisionerApiMetadata"

from serverless.app import app


client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer mock-test-token"}


def _list_all_vpcs():
    """Collect every VPC record across all pages of the paginated endpoint."""
    items, token = [], None
    while True:
        params = {"limit": 1000}
        if token:
            params["next_token"] = token
        response = client.get("/vpcs", headers=AUTH_HEADERS, params=params)
        assert response.status_code == 200
        body = response.json()
        items.extend(body["items"])
        token = body["next_token"]
        if not token:
            return items


def test_get_vpcs_empty():
    response = client.get("/vpcs", headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)
    assert "next_token" in body

def test_create_get_delete_vpc():
    # 1. Create VPC
    payload = {
        "vpc_cidr": "10.10.0.0/16",
        "subnets": [
            {"cidr_block": "10.10.1.0/24", "availability_zone": "us-east-1a"},
            {"cidr_block": "10.10.2.0/24", "availability_zone": "us-east-1b"}
        ]
    }
    response = client.post("/vpc", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 201
    data = response.json()
    assert data["message"] == "VPC, Subnets, Internet Gateway, and Route Table created successfully"
    vpc_id = data["data"]["VpcId"]
    assert vpc_id.startswith("vpc-")
    assert data["data"]["InternetGatewayId"].startswith("igw-")
    assert data["data"]["RouteTableId"].startswith("rtb-")
    assert len(data["data"]["Subnets"]) == 2

    # 2. Get Single VPC
    response = client.get(f"/vpc/{vpc_id}", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["VpcId"] == vpc_id

    # 3. List VPCs and find ours
    vpcs = _list_all_vpcs()
    assert any(v["VpcId"] == vpc_id for v in vpcs)

    # 4. Delete VPC
    response = client.delete(f"/vpc/{vpc_id}", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert "successfully deleted" in response.json()["message"]

    # 5. Check metadata is gone
    response = client.get(f"/vpc/{vpc_id}", headers=AUTH_HEADERS)
    assert response.status_code == 404

def test_unauthorized_access():
    # Calling endpoint without bearer token
    response = client.get("/vpcs")
    assert response.status_code == 401

    # Calling endpoint with invalid token
    response = client.get("/vpcs", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401

def test_create_vpc_invalid_cidr_rejected():
    # A malformed CIDR is now rejected by request validation (422) before any
    # AWS resource is created, rather than failing mid-provision.
    payload = {
        "vpc_cidr": "invalid-cidr",
        "subnets": []
    }
    response = client.post("/vpc", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 422


def test_iam_mode_skips_in_app_token_check(monkeypatch):
    import serverless.app as main

    # Under AUTH_MODE=iam the API Gateway authenticates callers via SigV4, so the
    # app must accept requests with no Bearer token (it trusts the gateway).
    monkeypatch.setattr(main, "AUTH_MODE", "iam")
    response = client.get("/vpcs")  # no Authorization header
    assert response.status_code == 200
    assert "items" in response.json()


def _raise_boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_create_vpc_rolls_back_on_failure(monkeypatch):
    import serverless.app as main

    # Force a mid-provision failure right after the VPC is created. Rollback
    # succeeds, so no tracking record should be persisted for this CIDR.
    monkeypatch.setattr(main.ec2, "create_internet_gateway", _raise_boom)

    payload = {
        "vpc_cidr": "10.50.0.0/16",
        "subnets": [{"cidr_block": "10.50.1.0/24", "availability_zone": "us-east-1a"}],
    }
    response = client.post("/vpc", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 500
    assert response.json()["detail"] == "Creation failed. Rollback initiated."

    # Clean rollback => nothing left tracked for this CIDR.
    leftovers = [v for v in _list_all_vpcs() if v.get("CidrBlock") == "10.50.0.0/16"]
    assert leftovers == []


def test_create_vpc_tracks_orphans_when_rollback_fails(monkeypatch):
    import serverless.app as main

    # Fail mid-provision AND fail the VPC teardown during rollback, so the
    # created VPC cannot be cleaned up and must be tracked for later removal.
    monkeypatch.setattr(main.ec2, "create_internet_gateway", _raise_boom)
    monkeypatch.setattr(main.ec2, "delete_vpc", _raise_boom)

    payload = {"vpc_cidr": "10.51.0.0/16", "subnets": []}
    response = client.post("/vpc", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 500

    incomplete = [
        v for v in _list_all_vpcs()
        if v.get("CidrBlock") == "10.51.0.0/16" and v.get("Status") == "ROLLBACK_INCOMPLETE"
    ]
    assert len(incomplete) >= 1
    assert incomplete[0].get("RollbackErrors")

    # Restore real clients and clean up the orphaned VPC + tracking record.
    monkeypatch.undo()
    for v in incomplete:
        try:
            main.ec2.delete_vpc(VpcId=v["VpcId"])
        except Exception:
            pass
        main.table.delete_item(Key={"VpcId": v["VpcId"]})
