"""CloudWatch Omni setup helpers for the L6 observability notebook.

Each function corresponds to one step in ``1_cloudwatch_omni.ipynb`` and is
idempotent — safe to call on re-runs. The notebook keeps the pedagogical
markdown; this module keeps the mechanics.

Public API:

    create_space_operator_role(iam, account_id)                 -> role_arn
    create_domain_and_space(omni, data_access_role_arn, region, account_id)
                                                                -> (domain_id, domain_url, space_id, space_arn)
    grant_space_admin(omni, cfn, domain_id, space_id)           -> granted_role_arn | None
    enable_telemetry_integrations(iam, sts, cw, obs_admin,
                                  account_id, region)           -> di_role_arn
"""
import hashlib
import json
import re
import time
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Step 3 — Space operator role (data access role) that Omni assumes to read
# this account's telemetry on your behalf.
# ---------------------------------------------------------------------------

# AWS-managed policies that together unlock every canvas on the Omni home.
# See the markdown in Step 3 of the notebook for what each one adds.
_OPERATOR_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/CloudWatchOmniSpaceAccessPolicy",      # base Space experience
    "arn:aws:iam::aws:policy/CloudWatchOmniAWSIntegrationPolicy",   # agent discovery / context graph
    "arn:aws:iam::aws:policy/CloudWatchOmniModelInferencePolicy",   # playground + evaluations
]


def create_space_operator_role(iam, account_id, role_name="CloudWatchOmniOperatorRole"):
    """Create (or reuse) the Omni Space operator role.

    Trusted by ``cloudwatch.amazonaws.com`` (the Omni service principal) and
    scoped to this account via ``aws:SourceAccount``, with the three AWS-managed
    policies attached. Returns the role ARN. Sleeps briefly after creation to
    let IAM eventual-consistency settle before ``create_space`` validates trust.
    """
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "cloudwatch.amazonaws.com"},
            "Action": [
                "sts:AssumeRole",
                "sts:TagSession",
                "sts:SetSourceIdentity",
                "sts:SetContext",
            ],
            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
        }],
    }

    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Data access role for the CloudWatch Omni Space (workshop)",
        )
        print(f"Created role: {role['Role']['Arn']}")
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=role_name)
        print(f"Reusing role: {role['Role']['Arn']}")
        created = False

    for policy_arn in _OPERATOR_MANAGED_POLICIES:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            print(f"Attached:    {policy_arn}")
        except iam.exceptions.NoSuchEntityException:
            print(f"[WARN] Managed policy not found: {policy_arn}")
            print("        If this is a newly-launched policy, check the Omni docs for the current ARN.")

    if created:
        # Brief wait for IAM eventual consistency.
        time.sleep(10)

    return role["Role"]["Arn"]


# ---------------------------------------------------------------------------
# Step 4 — Account Domain + Space.
# ---------------------------------------------------------------------------

def create_domain_and_space(
    omni,
    data_access_role_arn,
    region,
    account_id,
    domain_name=None,
    space_name=None,
    tags=None,
):
    """Create (or reuse) the Omni Account Domain and the Space inside it.

    Returns ``(domain_id, domain_url, space_id, space_arn)``. Omni allows one
    domain per account/region, so this reuses an existing active domain if
    present. ``clientToken`` is derived from account/name so retries do not
    fan out into duplicates. Falls back to listing on ``ConflictException``.

    ``domain_name`` defaults to ``anycompany-<last-4-of-account-id>`` to avoid
    collisions when the account previously had a domain with that name deleted
    (Omni reserves the name even after deletion).
    """
    # Derive unique-enough default names from the account ID so a deleted
    # domain's reserved name doesn't block re-creation.
    if domain_name is None:
        domain_name = f"anycompany-{account_id[-4:]}"
    if space_name is None:
        space_name = f"anycompany-{account_id[-4:]}"

    tags = tags or {"workshop": "anycompany-pluggable", "layer": "l6-observability"}

    dom_tok = hashlib.sha1(f"{account_id}:{domain_name}".encode()).hexdigest()[:32]
    space_tok = hashlib.sha1(f"{account_id}:{region}:{space_name}".encode()).hexdigest()[:32]

    # ---- Domain ----
    # list_domains returns {"items": [...]} summaries; full details (incl.
    # sign-in URL) require a get_domain call.
    # Note: do NOT filter on status=="ACTIVE" — a domain in CREATING/FAILED
    # state still blocks create_domain with ConflictException. Reuse any
    # existing domain regardless of its current state.
    existing = omni.list_domains().get("items", [])
    if existing:
        domain = omni.get_domain(domainId=existing[0]["domainId"])["domain"]
        print(f"Reusing existing domain: {domain['name']} ({domain['domainId']})")
    else:
        try:
            dr = omni.create_domain(
                name=domain_name,
                identityProviders=["IAM"],
                clientToken=dom_tok,
            )
            domain = dr["domain"]
            print(f"Created domain: {domain['name']} ({domain['domainId']})")
        except (omni.exceptions.ConflictException, ClientError):
            # Race condition or name already reserved — fetch whatever exists.
            all_domains = omni.list_domains().get("items", [])
            if not all_domains:
                raise
            domain = omni.get_domain(domainId=all_domains[0]["domainId"])["domain"]
            print(f"Reusing domain (conflict fallback): {domain['name']} ({domain['domainId']})")

    domain_id = domain["domainId"]
    domain_url = (domain.get("customEndpointUrls") or [None])[0] or domain.get("domainEndpointUrl")

    # ---- Space ----
    try:
        sr = omni.create_space(
            domainId=domain_id,
            name=space_name,
            dataAccessRoleArn=data_access_role_arn,
            clientToken=space_tok,
            tags=tags,
        )
        space = sr["space"]
        print(f"Created space: {space['spaceId']}")
    except (omni.exceptions.ConflictException, ClientError):
        found = None
        for page in omni.get_paginator("list_spaces").paginate(domainId=domain_id):
            for s in page.get("items", []):
                if s.get("name") == space_name and s.get("region") == region:
                    found = s
                    break
            if found:
                break
        if not found:
            raise
        space = omni.get_space(spaceId=found["spaceId"])["space"]
        print(f"Reusing space: {space['spaceId']}")

    return domain_id, domain_url, space["spaceId"], space["spaceArn"]


# ---------------------------------------------------------------------------
# Step 5 — SPACE_ADMIN grant for the console identity.
# ---------------------------------------------------------------------------

# arn:aws:sts::ACCT:assumed-role/RoleName/Session -> arn:aws:iam::ACCT:role/RoleName
_STS_ASSUMED_RE = re.compile(r"arn:aws:sts::(\d+):assumed-role/([^/]+)/")


def grant_space_admin(omni, cfn, domain_id, space_id, stack_name="agent-stack"):
    """Grant SPACE_ADMIN on the Space to the deploying console role.

    Reads ``ParticipantRoleArn`` from the ``agent-stack`` CFN stack params (that
    is the ARN of whoever ran ``deploy-stack.sh``), normalizes an STS assumed-role
    ARN to the underlying IAM role ARN, and creates an IAM_ROLE access grant.
    Idempotent via ``clientToken``. Returns the role ARN granted, or ``None``
    when the stack parameter isn't present (caller should add the grant manually).
    """
    stack_params = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Parameters"]
    caller_arn = next(
        (p["ParameterValue"] for p in stack_params if p["ParameterKey"] == "ParticipantRoleArn"),
        None,
    )

    if not caller_arn:
        print("[WARN] ParticipantRoleArn not found in CFN parameters — skipping auto-grant.")
        print("       Add your console role manually: Omni -> Space admins -> Add roles")
        return None

    m = _STS_ASSUMED_RE.match(caller_arn)
    role_arn = f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" if m else caller_arn
    grant_token = f"grant-{hashlib.sha1(role_arn.encode()).hexdigest()[:16]}"

    try:
        omni.create_access_grant(
            domainId=domain_id,
            spaceId=space_id,
            name="workshop-console-admin",
            permission="SPACE_ADMIN",
            principal={"principalType": "IAM_ROLE", "principalId": role_arn},
            clientToken=grant_token,
        )
        print(f"Granted SPACE_ADMIN to {role_arn}")
    except omni.exceptions.ConflictException:
        print(f"SPACE_ADMIN grant already exists for {role_arn}")
    except Exception as e:
        print(f"[WARN] Could not create access grant: {e}")
        print(f"       Add this role manually: Omni -> Space admins -> Add roles -> {role_arn}")

    return role_arn


# ---------------------------------------------------------------------------
# Step 6 — Space telemetry integrations.
# ---------------------------------------------------------------------------
#
# create_space wires up identity (Domain + Space + grants) but does NOT turn
# on telemetry ingestion. The console Space wizard quietly provisions three
# things the API skips:
#
# 1. Dataset Integration — CloudWatch Logs -> Omni Dataset. Needs its OWN
#    role, NOT the Space's dataAccessRoleArn, with logs:IntegrateWithDataset
#    + cloudwatch:PutRecords, trusted by logs.amazonaws.com.
# 2. Resource-tag telemetry enrichment (observabilityadmin:StartTelemetry
#    Enrichment). Nice-to-have; blocked at the service level in some sandbox
#    accounts, we continue gracefully.
# 3. OTel enrichment for AWS Metrics (cloudwatch:StartOtelEnrichment).
# ---------------------------------------------------------------------------


def _dataset_integration_role(iam, account_id, role_name):
    """Create (or reuse) the Dataset Integration role and refresh its inline policy."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "CloudWatchLogsAssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "logs.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:observabilityadmin:*:{account_id}:dataset-integration/*",
                },
            },
        }],
    }
    try:
        r = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Lets CloudWatch Logs copy log groups into the Omni default Dataset.",
        )
        role_arn = r["Role"]["Arn"]
        print(f"Created role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        print(f"Reusing role: {role_arn}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="OmniDatasetIntegrationPolicy",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "IntegrateLogGroupsWithDataset",
                    "Effect": "Allow",
                    "Action": "logs:IntegrateWithDataset",
                    "Resource": f"arn:aws:logs:*:{account_id}:log-group:*",
                },
                {
                    "Sid": "PutRecordsToDataset",
                    "Effect": "Allow",
                    "Action": "cloudwatch:PutRecords",
                    "Resource": f"arn:aws:cloudwatch:*:{account_id}:dataset/*",
                },
            ],
        }),
    )
    return role_arn


def _start_telemetry_enrichment(obs_admin):
    try:
        obs_admin.start_telemetry_enrichment()
        print("Started telemetry enrichment (resource tags on telemetry)")
    except obs_admin.exceptions.ConflictException:
        print("Telemetry enrichment already running")
    except obs_admin.exceptions.AccessDeniedException as e:
        # Some sandboxes block this at the service level; the Agents view
        # does not need it, so continue.
        print(f"[SKIP] Telemetry enrichment blocked at service level — not required "
              f"for the Agents view. ({e.response['Error']['Message']})")
    except Exception as e:
        print(f"[WARN] StartTelemetryEnrichment: {e}")


def _start_otel_enrichment(cw):
    try:
        cw.start_otel_enrichment()
        print("Started OTel enrichment for AWS Metrics")
    except cw.exceptions.ValidationError as e:
        print(f"OTel enrichment already running (or validation issue): {e}")
    except Exception as e:
        print(f"[WARN] StartOTelEnrichment: {e}")


def _create_dataset_integration(obs_admin, di_role_arn, tags):
    try:
        resp = obs_admin.create_dataset_integration(RoleArn=di_role_arn, Tags=tags)
        print(f"Created dataset integration: {resp['Arn']}")
        return
    except obs_admin.exceptions.ConflictException:
        existing = obs_admin.list_dataset_integrations().get("DatasetIntegrationSummaries", [])
        if existing:
            print(f"Dataset integration already exists: {existing[0].get('Arn')}")
        else:
            print("Dataset integration ConflictException but list returned empty")
        return
    except obs_admin.exceptions.AccessDeniedException as e:
        msg = e.response["Error"]["Message"]
        if "TagResource" not in msg:
            print(f"[WARN] CreateDatasetIntegration: {e}")
            return
        print("[INFO] TagResource blocked, retrying without Tags...")
        try:
            resp = obs_admin.create_dataset_integration(RoleArn=di_role_arn)
            print(f"Created dataset integration (no tags): {resp['Arn']}")
        except Exception as e2:
            print(f"[WARN] CreateDatasetIntegration (no tags): {e2}")
    except Exception as e:
        print(f"[WARN] CreateDatasetIntegration: {e}")


def enable_telemetry_integrations(
    iam,
    sts,
    cw,
    obs_admin,
    account_id,
    region,
    di_role_name="CloudWatchOmniDatasetIntegrationRole",
    tags=None,
):
    """Create the Dataset Integration role, start both enrichment pipelines, and
    create the Dataset Integration itself. The notebook role's observabilityadmin
    and PassRole permissions come from the workshop CloudFormation stack. Returns the Dataset Integration role ARN.

    Every call is wrapped in idempotent error handling so re-runs are safe.
    """
    tags = tags or {"workshop": "anycompany-pluggable", "layer": "l6-observability"}

    di_role_arn = _dataset_integration_role(iam, account_id, di_role_name)

    # IAM propagation delay before the new Dataset Integration role is used.
    time.sleep(15)

    _start_telemetry_enrichment(obs_admin)
    _start_otel_enrichment(cw)
    _create_dataset_integration(obs_admin, di_role_arn, tags)
    return di_role_arn


# ---------------------------------------------------------------------------
# One-call orchestrator — chains the four steps above.
# ---------------------------------------------------------------------------

def setup(
    region,
    account_id,
    stack_name="agent-stack",
    role_name="CloudWatchOmniOperatorRole",
    domain_name=None,
    space_name=None,
    di_role_name="CloudWatchOmniDatasetIntegrationRole",
    tags=None,
):
    """Set up CloudWatch Omni end-to-end.

    Chains the four lower-level functions in order:
      1. ``create_space_operator_role``       — IAM role the Space assumes
      2. ``create_domain_and_space``          — Omni Account Domain + Space
      3. ``grant_space_admin``                — SPACE_ADMIN for the console caller
      4. ``enable_telemetry_integrations``    — Dataset Integration + enrichments

    All boto3 clients are created internally so the notebook only has to pass
    ``region`` and ``account_id``. Every underlying call is idempotent, so this
    function is safe to re-run.

    Returns a dict with the IDs/ARNs/URLs the caller might want to display:

        {
          "data_access_role_arn": ...,
          "domain_id":            ...,
          "domain_url":           ...,
          "space_id":             ...,
          "space_arn":            ...,
          "granted_role_arn":     ...,  # or None if ParticipantRoleArn wasn't set
          "di_role_arn":          ...,
        }
    """
    import boto3

    iam = boto3.client("iam")
    sts = boto3.client("sts")
    omni = boto3.client("cloudwatchomni", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    obs_admin = boto3.client("observabilityadmin", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)

    data_access_role_arn = create_space_operator_role(iam, account_id, role_name=role_name)

    domain_id, domain_url, space_id, space_arn = create_domain_and_space(
        omni,
        data_access_role_arn=data_access_role_arn,
        region=region,
        account_id=account_id,
        domain_name=domain_name,
        space_name=space_name,
        tags=tags,
    )

    granted_role_arn = grant_space_admin(omni, cfn, domain_id, space_id, stack_name=stack_name)

    di_role_arn = enable_telemetry_integrations(
        iam=iam,
        sts=sts,
        cw=cw,
        obs_admin=obs_admin,
        account_id=account_id,
        region=region,
        di_role_name=di_role_name,
        tags=tags,
    )

    return {
        "data_access_role_arn": data_access_role_arn,
        "domain_id":            domain_id,
        "domain_url":           domain_url,
        "space_id":             space_id,
        "space_arn":            space_arn,
        "granted_role_arn":     granted_role_arn,
        "di_role_arn":          di_role_arn,
    }


# ---------------------------------------------------------------------------
# Teardown — symmetric counterpart to setup(). Deletes everything the four
# setup functions create, in reverse dependency order. Every call is wrapped
# in idempotent error handling so a partial state (e.g. only some resources
# present from a half-run setup) is safe to tear down.
# ---------------------------------------------------------------------------


def _delete_access_grants(omni, domain_id, space_id):
    """Delete non-system access grants on the Space. System grants cannot be
    deleted and will raise ConflictException - we skip them."""
    try:
        grants = omni.list_access_grants(domainId=domain_id, spaceId=space_id).get("items", [])
    except Exception as e:
        print(f"[WARN] list_access_grants: {e}")
        return
    for g in grants:
        gid = g.get("grantId")
        try:
            omni.delete_access_grant(grantId=gid)
            print(f"Deleted access grant: {gid}")
        except omni.exceptions.ConflictException:
            # system-managed grant; expected
            pass
        except Exception as e:
            print(f"[WARN] delete_access_grant({gid}): {e}")


def _delete_dataset_integration(obs_admin):
    """Delete the account-level Dataset Integration(s) if present.

    list_dataset_integrations returns ``DatasetIntegrationSummaries`` (not
    ``DatasetIntegrations``, which is what create_dataset_integration's return
    shape suggests). Each summary has an ``Arn`` field.
    """
    try:
        integrations = obs_admin.list_dataset_integrations().get("DatasetIntegrationSummaries", [])
    except Exception as e:
        print(f"[WARN] list_dataset_integrations: {e}")
        return
    for di in integrations:
        arn = di.get("Arn")
        if not arn:
            continue
        try:
            obs_admin.delete_dataset_integration(Arn=arn)
            print(f"Deleted dataset integration: {arn}")
        except Exception as e:
            print(f"[WARN] delete_dataset_integration({arn}): {e}")


def _delete_role_with_policies(iam, role_name):
    """Detach all managed policies, delete all inline policies, delete the role."""
    try:
        iam.get_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        print(f"Role {role_name} already gone")
        return
    try:
        for p in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        for pn in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role_name, PolicyName=pn)
        iam.delete_role(RoleName=role_name)
        print(f"Deleted role:  {role_name}")
    except Exception as e:
        print(f"[WARN] deleting role {role_name}: {e}")


def teardown(
    region,
    account_id,
    delete_domain=True,
    stop_enrichments=False,
    role_name="CloudWatchOmniOperatorRole",
    di_role_name="CloudWatchOmniDatasetIntegrationRole",
):
    """Delete everything ``setup()`` creates, in reverse dependency order.

    Order:
      1. non-system access grants on the Space
      2. the Space
      3. the Domain (optional — only if ``delete_domain=True``)
      4. the Dataset Integration
      5. telemetry + OTel enrichments (optional — only if ``stop_enrichments=True``;
         these are account-level and often shared with other work)
      6. ``CloudWatchOmniDatasetIntegrationRole`` + its inline policy
      7. ``CloudWatchOmniOperatorRole`` + its managed policies

    Every step is wrapped in idempotent error handling. Safe to re-run.
    """
    import boto3

    iam = boto3.client("iam")
    sts = boto3.client("sts")
    omni = boto3.client("cloudwatchomni", region_name=region)
    obs_admin = boto3.client("observabilityadmin", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)

    # Find the active domain + space (if any) so cleanup works even when the
    # notebook variables are stale from a prior run.
    try:
        active = [d for d in omni.list_domains().get("items", []) if d.get("status") == "ACTIVE"]
    except Exception as e:
        print(f"[WARN] list_domains: {e}")
        active = []

    for d in active:
        domain_id = d["domainId"]
        # Walk every space in this domain.
        try:
            spaces_iter = omni.get_paginator("list_spaces").paginate(domainId=domain_id)
        except Exception as e:
            print(f"[WARN] list_spaces({domain_id}): {e}")
            continue
        for page in spaces_iter:
            for s in page.get("items", []):
                space_id = s["spaceId"]
                _delete_access_grants(omni, domain_id, space_id)
                try:
                    omni.delete_space(spaceId=space_id)
                    print(f"Deleted space: {space_id}")
                except Exception as e:
                    print(f"[WARN] delete_space({space_id}): {e}")
        if delete_domain:
            try:
                omni.delete_domain(domainId=domain_id)
                print(f"Deleted domain: {domain_id}")
            except Exception as e:
                print(f"[WARN] delete_domain({domain_id}): {e}")

    _delete_dataset_integration(obs_admin)

    if stop_enrichments:
        try:
            obs_admin.stop_telemetry_enrichment()
            print("Stopped telemetry enrichment")
        except Exception as e:
            print(f"[WARN] stop_telemetry_enrichment: {e}")
        try:
            cw.stop_otel_enrichment()
            print("Stopped OTel enrichment")
        except Exception as e:
            print(f"[WARN] stop_otel_enrichment: {e}")

    _delete_role_with_policies(iam, di_role_name)
    _delete_role_with_policies(iam, role_name)
    print()
    print("CloudWatch Omni resources cleaned up.")
