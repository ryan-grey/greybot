"""Mythic+ state in greyBot's existing DynamoDB table; no public data bucket state."""
import json
import time
import uuid
from botocore.exceptions import ClientError


class Repository:
    def __init__(self, client, table, guild):
        self.client, self.table, self.pk = client, table, "MPLUS#" + guild

    def key(self, key):
        return {"pk":{"S":self.pk}, "sk":{"S":key}}

    def get(self, key):
        item = self.client.get_item(TableName=self.table, Key=self.key(key), ConsistentRead=True).get("Item", {})
        return json.loads(item["body"]["S"]) if item.get("body") else None

    def put(self, key, body, once=False):
        args = {"TableName":self.table, "Item":{**self.key(key), "body":{"S":json.dumps(body, ensure_ascii=False)}}}
        if once:
            args["ConditionExpression"] = "attribute_not_exists(pk)"
        try:
            self.client.put_item(**args)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def prefix(self, prefix):
        result, cursor = [], None
        while True:
            args = {"TableName":self.table, "KeyConditionExpression":"pk=:p AND begins_with(sk,:s)",
                    "ExpressionAttributeValues":{":p":{"S":self.pk}, ":s":{"S":prefix}}, "ConsistentRead":True}
            if cursor:
                args["ExclusiveStartKey"] = cursor
            response = self.client.query(**args)
            result.extend(json.loads(row["body"]["S"]) for row in response.get("Items",[]) if row.get("body"))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return result

    def after(self, prefix, cursor, limit=50):
        """One bounded queue page; the saved key is excluded on resume."""
        response=self.client.query(TableName=self.table,
            KeyConditionExpression='pk=:p AND sk BETWEEN :a AND :z',
            ExpressionAttributeValues={':p':{'S':self.pk},':a':{'S':cursor or prefix},':z':{'S':prefix+'\uffff'}},
            ConsistentRead=True,Limit=limit+1)
        return [(row['sk']['S'],json.loads(row['body']['S'])) for row in response.get('Items',[])
                if row['sk']['S'] > (cursor or prefix)][:limit]

    def lease(self, key="LEASE"):
        owner, now = uuid.uuid4().hex, int(time.time())
        try:
            self.client.update_item(TableName=self.table, Key=self.key(key),
                UpdateExpression="SET #expires=:e, #owner=:o", ConditionExpression="attribute_not_exists(#expires) OR #expires < :n",
                ExpressionAttributeNames={"#expires":"expires", "#owner":"owner"},
                ExpressionAttributeValues={":e":{"N":str(now+90)},":n":{"N":str(now)},":o":{"S":owner}})
            return owner
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def release(self, owner, key="LEASE"):
        self.client.update_item(TableName=self.table, Key=self.key(key), UpdateExpression="SET #expires=:e",
            ConditionExpression="#owner=:o", ExpressionAttributeNames={"#expires":"expires", "#owner":"owner"},
            ExpressionAttributeValues={":e":{"N":"0"},":o":{"S":owner}})
