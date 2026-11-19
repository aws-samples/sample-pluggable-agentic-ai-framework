import json, os, boto3, logging
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AGENT_REGION", "us-east-1")
ORDERS_TABLE = os.environ.get("ORDERS_TABLE", "CustomerOrders")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
orders_table = dynamodb.Table(ORDERS_TABLE)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _serialize(data):
    return json.loads(json.dumps(data, cls=DecimalEncoder))


def lambda_handler(event, context):
    logger.info(f"Processing request - order_id present: {bool(event.get('order_id'))}, customer_id present: {bool(event.get('customer_id'))}")

    order_id = event.get("order_id", "")
    customer_id = event.get("customer_id", "")

    try:
        # Case 1: Lookup by order_id (uses GSI OrderIdIndex)
        if order_id:
            from boto3.dynamodb.conditions import Key
            resp = orders_table.query(
                IndexName="OrderIdIndex",
                KeyConditionExpression=Key("orderId").eq(order_id),
            )
            items = resp.get("Items", [])
            if not items:
                result = {"status": "not_found", "message": f"No order found with ID {order_id}"}
            else:
                result = {"status": "success", "order": _serialize(items[0])}

        # Case 2: Lookup by customer_id (partition key)
        elif customer_id:
            from boto3.dynamodb.conditions import Key
            resp = orders_table.query(
                KeyConditionExpression=Key("customerId").eq(customer_id),
            )
            items = resp.get("Items", [])
            if not items:
                result = {"status": "not_found", "message": f"No orders found for customer {customer_id}"}
            else:
                result = {
                    "status": "success",
                    "customer_id": customer_id,
                    "order_count": len(items),
                    "orders": _serialize(items),
                }

        else:
            result = {"status": "error", "message": "Provide at least one of: order_id or customer_id."}

    except Exception as e:
        logger.error(f"Error: {e}")
        result = {"status": "error", "message": str(e)}

    logger.info(f"Request completed - status: {result.get('status')}, record_count: {len(result.get('orders', [result.get('order')] if result.get('order') else []))}")
    return result
