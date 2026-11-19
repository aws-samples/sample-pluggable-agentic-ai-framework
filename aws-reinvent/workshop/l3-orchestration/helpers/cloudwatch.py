"""CloudWatch vended-log and trace delivery helpers for the L3 orchestration notebooks.

Wires an AgentCore resource (runtime, memory, gateway) to CloudWatch Logs:

    delivery source (resource + log type) -> delivery -> delivery destination -> log group

and registers a TRACES delivery source, optionally delivered to the account's
shared X-Ray destination. Every step is idempotent, so cells can be re-run.

This is the CloudWatch provider. Other providers (for example Datadog) live in
their own module alongside this one; see ``datadog.py``.
"""

# Per-resource settings: name prefix, vended-log group base, default log types,
# and whether the TRACES source gets a delivery to the shared X-Ray destination.
KINDS = {
    "runtime": {
        "prefix": "rt",
        "log_group_base": "/aws/vendedlogs/bedrock-agentcore/runtime",
        "log_types": ["APPLICATION_LOGS", "USAGE_LOGS"],
        "deliver_traces_to_xray": False,
    },
    "memory": {
        "prefix": "mem",
        "log_group_base": "/aws/vendedlogs/bedrock-agentcore/memory",
        "log_types": ["APPLICATION_LOGS"],
        "deliver_traces_to_xray": True,
    },
    "gateway": {
        "prefix": "gw",
        "log_group_base": "/aws/vendedlogs/bedrock-agentcore/gateway",
        "log_types": ["APPLICATION_LOGS"],
        "deliver_traces_to_xray": True,
    },
}


def _find_source(logs_client, resource_arn, log_type):
    """Return the name of an existing delivery source for this resource + log type."""
    for s in logs_client.describe_delivery_sources().get("deliverySources", []):
        if resource_arn in s.get("resourceArns", []) and s.get("logType") == log_type:
            return s["name"]
    return None


def enable_log_delivery(logs_client, resource_arn, resource_id, prefix, log_group_base,
                        log_types, account_id):
    """Deliver each log type for a resource to its own CloudWatch log group."""
    region = logs_client.meta.region_name
    for log_type in log_types:
        source_name = f"{prefix}-{resource_id[:20]}-{log_type.lower()}"
        dest_name = f"{source_name}-dest"
        dest_log_group = f"{log_group_base}/{log_type}/{resource_id}"

        # 1. Delivery source (reuse one that already exists for this resource)
        existing = _find_source(logs_client, resource_arn, log_type)
        if existing:
            source_name = existing
            print(f"✓ {log_type} source already exists: {source_name}")
        else:
            try:
                logs_client.put_delivery_source(name=source_name, resourceArn=resource_arn, logType=log_type)
                print(f"✓ Created {log_type} source: {source_name}")
            except Exception as e:
                print(f"✗ {log_type} source: {e}")

        # 2. Log group
        try:
            logs_client.create_log_group(logGroupName=dest_log_group)
        except logs_client.exceptions.ResourceAlreadyExistsException:
            pass

        # 3. Delivery destination pointing at the log group
        try:
            logs_client.put_delivery_destination(
                name=dest_name,
                deliveryDestinationConfiguration={
                    "destinationResourceArn": f"arn:aws:logs:{region}:{account_id}:log-group:{dest_log_group}"
                },
            )
        except logs_client.exceptions.ConflictException:
            pass

        # 4. Delivery linking source -> destination
        try:
            logs_client.create_delivery(
                deliverySourceName=source_name,
                deliveryDestinationArn=f"arn:aws:logs:{region}:{account_id}:delivery-destination:{dest_name}",
            )
            print(f"✓ {log_type} delivery created")
        except logs_client.exceptions.ConflictException:
            print(f"✓ {log_type} delivery already exists")
        except Exception as e:
            print(f"✗ {log_type} delivery: {e}")


def enable_tracing(logs_client, resource_arn, resource_id, prefix, deliver_to_xray=False):
    """Register a TRACES source and, if asked, deliver it to the shared X-Ray destination."""
    trace_source_name = f"{prefix}-{resource_id[:20]}-traces"
    try:
        existing = _find_source(logs_client, resource_arn, "TRACES")
        if existing:
            trace_source_name = existing
            print("✓ Trace source exists")
        else:
            logs_client.put_delivery_source(name=trace_source_name, resourceArn=resource_arn, logType="TRACES")
            print("✓ Trace source created")
        if not deliver_to_xray:
            return
        # The X-Ray destination is created when the first agent runtime is deployed
        xray_dest_arn = next(
            (d["arn"] for d in logs_client.describe_delivery_destinations().get("deliveryDestinations", [])
             if d.get("deliveryDestinationType") == "XRAY"),
            None,
        )
        if not xray_dest_arn:
            print("  ⚠ No XRAY destination found. Run the agent notebooks first.")
            return
        try:
            logs_client.create_delivery(deliverySourceName=trace_source_name, deliveryDestinationArn=xray_dest_arn)
            print("✓ Tracing delivery created")
        except logs_client.exceptions.ConflictException:
            print("✓ Tracing delivery already exists")
    except Exception as e:
        print(f"✗ Tracing: {e}")


def enable_observability(logs_client, kind, resource_arn, resource_id, account_id,
                         log_types=None):
    """Enable log delivery and tracing for an AgentCore runtime, memory, or gateway."""
    cfg = KINDS[kind]
    enable_log_delivery(
        logs_client, resource_arn, resource_id, cfg["prefix"], cfg["log_group_base"],
        log_types or cfg["log_types"], account_id,
    )
    enable_tracing(logs_client, resource_arn, resource_id, cfg["prefix"], cfg["deliver_traces_to_xray"])
    print(f"\n{kind.capitalize()} observability configured for {resource_id}")


# Convenience alias so notebook snippets can read ``cloudwatch.enable(...)``.
enable = enable_observability


def disable_log_delivery(logs_client, resource_arn, dest_name_prefix):
    """Delete the deliveries, destinations, and sources created for a resource.

    Only destinations whose name starts with ``dest_name_prefix`` are deleted, and
    the shared XRAY destination is never touched.
    """
    if not resource_arn:
        return
    # 1. Sources tied to this resource
    sources_for_res = []
    try:
        for page in logs_client.get_paginator("describe_delivery_sources").paginate():
            sources_for_res += [s for s in page.get("deliverySources", []) if resource_arn in s.get("resourceArns", [])]
    except Exception as e:
        print(f"List delivery sources for {resource_arn}: {e}")
        return
    source_names = {s["name"] for s in sources_for_res}

    # 2. Deliveries from those sources (remember their destinations)
    dest_arns_seen = set()
    if source_names:
        try:
            for page in logs_client.get_paginator("describe_deliveries").paginate():
                for d in page.get("deliveries", []):
                    if d.get("deliverySourceName") not in source_names:
                        continue
                    if d.get("deliveryDestinationArn"):
                        dest_arns_seen.add(d["deliveryDestinationArn"])
                    try:
                        logs_client.delete_delivery(id=d["id"])
                        print(f"Deleted delivery: {d['id']}")
                    except Exception as e:
                        print(f"Delete delivery ({d['id']}): {e}")
        except Exception as e:
            print(f"List deliveries: {e}")

    # 3. Destinations we own (name prefix match, never XRAY)
    try:
        dests_by_arn = {d["arn"]: d for d in logs_client.describe_delivery_destinations().get("deliveryDestinations", [])}
    except Exception as e:
        print(f"List delivery destinations: {e}")
        dests_by_arn = {}
    for dest_arn in dest_arns_seen:
        d = dests_by_arn.get(dest_arn)
        if not d or d.get("deliveryDestinationType") == "XRAY":
            continue
        dname = d.get("name", "")
        if not dname.startswith(dest_name_prefix):
            continue
        try:
            logs_client.delete_delivery_destination(name=dname)
            print(f"Deleted delivery destination: {dname}")
        except Exception as e:
            print(f"Delete delivery destination ({dname}): {e}")

    # 4. Sources
    for s in sources_for_res:
        try:
            logs_client.delete_delivery_source(name=s["name"])
            print(f"Deleted delivery source: {s['name']}")
        except Exception as e:
            print(f"Delete delivery source ({s['name']}): {e}")
