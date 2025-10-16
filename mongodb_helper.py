import traceback
from math import ceil

import motor.motor_asyncio
from bson import ObjectId
from typing import List, Optional, Dict, Any, Tuple

from pymongo.results import UpdateResult


class MongoDBHelper:
    _client = None

    def __init__(self, db_name: str, uri: str = 'mongodb://localhost:27017', max_pool_size: int = 5000):
        if MongoDBHelper._client is None:
            MongoDBHelper._client = motor.motor_asyncio.AsyncIOMotorClient(
                uri,
                maxPoolSize=max_pool_size,
                serverSelectionTimeoutMS=5000,
                socketTimeoutMS=10000,
                connectTimeoutMS=10000,
                maxIdleTimeMS=300000
            )
            print("Initialized MongoDB Client:")
            # traceback.print_stack()  # 打印堆栈信息，确认来源
        self.db = MongoDBHelper._client[db_name]

    async def insert_one(self, collection_name: str, data: Dict[str, Any]) -> str:
        data['_id'] = str(ObjectId())
        result = await self.db[collection_name].insert_one(data)
        return str(result.inserted_id)

    async def insert_many(self, collection_name: str, data_list: List[Dict[str, Any]]) -> List[str]:
        for data in data_list:
            data['_id'] = str(ObjectId())
        result = await self.db[collection_name].insert_many(data_list)
        return [str(id) for id in result.inserted_ids]

    async def aggregate(self, collection_name: str, pipeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        执行聚合操作
        :param collection_name: 集合名
        :param pipeline: 聚合管道列表
        :return: 聚合结果列表
        """
        collection = self.db[collection_name]
        cursor = collection.aggregate(pipeline)
        return [doc async for doc in cursor]

    async def find_one(self, collection_name: str, query: Dict[str, Any], projection: Dict[str, Any] = None) -> \
            Optional[Dict[str, Any]]:
        document = await self.db[collection_name].find_one(query, projection=projection)
        return document

    async def find_many(
            self,
            collection_name: str,
            query: Dict[str, Any],
            projection: Dict[str, Any] = None,
            sort: List[Tuple[str, int]] = None,
            skip: int = 0,
            limit: int = 0
    ) -> List[Dict[str, Any]]:
        cursor = self.db[collection_name].find(query, projection=projection)

        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)

        documents = await cursor.to_list(length=None if limit == 0 else limit)
        return documents

    async def find_all(self, collection_name: str):
        cursor = self.db[collection_name].find()
        users = []
        async for document in cursor:
            users.append(document)
        return users

    async def get_latest_content(self, collection_name: str, query: Dict[str, Any]) -> Optional[str]:
        """
        获取上传数组中最后一条记录的content字段
        """
        # 使用 MongoDB 的 $slice 操作符获取 uploads 数组的最后一条记录
        projection = {"uploads": {"$slice": -1}}  # 获取最后一条记录
        document = await self.db[collection_name].find_one(query, projection=projection)

        if document and 'uploads' in document and document['uploads']:
            # 返回最后一条记录的 content 字段
            return document['uploads'][-1].get('content', '')

        return None

    async def update_with_operators(self, collection_name: str, query: Dict[str, Any],
                                    update_data: Dict[str, Any]) -> bool:
        result = await self.db[collection_name].update_one(query, update_data, upsert=True)
        return result.modified_count > 0

    async def update_one(self, collection_name: str, query: Dict[str, Any], update_data: Dict[str, Any]) -> bool:
        result = await self.db[collection_name].update_one(query, {"$set": update_data}, upsert=True)
        return result.modified_count > 0

    async def update_push(self, collection_name: str, query: Dict[str, Any], update_data: Dict[str, Any]) -> bool:
        result = await self.db[collection_name].update_one(query, {"$push": update_data}, upsert=True)
        return result.modified_count > 0

    async def update(self,
                     collection_name: str,
                     query: Dict[str, Any],
                     update_data: Dict[str, Any],
                     array_filters: Optional[List[Dict[str, Any]]] = None
                     ) -> UpdateResult:
        if array_filters:
            result = await self.db[collection_name].update_one(
                query, update_data, upsert=True, array_filters=array_filters
            )
        else:
            result = await self.db[collection_name].update_one(
                query, update_data, upsert=True
            )
        return result

    # async def find_with_pagination(
    #         self,
    #         collection_name: str,
    #         query: Dict[str, Any],
    #         page: int = 1,
    #         page_size: int = 10,
    #         sort: List[Tuple[str, int]] = None
    # ) -> Dict[str, Any]:
    #     """
    #     Fetch documents with pagination
    #
    #     Args:
    #         collection_name: Collection to query
    #         query: MongoDB query dict
    #         page: Page number (starts from 1)
    #         page_size: Number of items per page
    #         sort: List of tuples with field name and direction [(field_name, direction)]
    #             direction: 1 for ascending, -1 for descending
    #     """
    #     skip = (page - 1) * page_size
    #     cursor = self.db[collection_name].find(query)
    #
    #     if sort:
    #         # 将列表转换为符合 MongoDB `sort()` 方法要求的格式
    #         cursor = cursor.sort(sort)  # sort 应该直接传入一个列表的元组（字段名, 排序方式）
    #
    #     total = await self.db[collection_name].count_documents(query)
    #     documents = await cursor.skip(skip).limit(page_size).to_list(None)
    #
    #     return {
    #         "items": documents,
    #         "total": total,
    #         "page": page,
    #         "page_size": page_size,
    #         "pages": ceil(total / page_size)
    #     }

    async def find_with_pagination(
            self,
            collection_name: str,
            query: Dict[str, Any],
            page: int = 1,
            page_size: int = 10,
            sort: List[Tuple[str, int]] = None
    ) -> Dict[str, Any]:
        """
        Fetch documents with pagination

        Args:
            collection_name: Collection to query
            query: MongoDB query dict
            page: Page number (starts from 1)
            page_size: Number of items per page
            sort: List of tuples with field name and direction [(field_name, direction)]
                direction: 1 for ascending, -1 for descending
        """
        skip = (page - 1) * page_size
        cursor = self.db[collection_name].find(query)

        if sort:
            cursor = cursor.sort(sort)

        total = await self.db[collection_name].count_documents(query)
        documents = await cursor.skip(skip).limit(page_size).to_list(None)

        # 解决 ObjectId 无法被序列化的问题
        for doc in documents:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])  # 转换为字符串

        return {
            "items": documents,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": ceil(total / page_size)
        }

    async def update_many(self, collection_name: str, query: Dict[str, Any], update_data: Dict[str, Any]) -> bool:
        """
        批量更新符合条件的多个文档

        :param collection_name: 集合名称
        :param query: 查询条件
        :param update_data: 更新操作数据
        :return: 如果更新了文档，返回 True，否则返回 False
        """
        result = await self.db[collection_name].update_many(query, {"$set": update_data})
        return result.modified_count > 0

    async def delete_one(self, collection_name: str, query: Dict[str, Any]) -> bool:
        result = await self.db[collection_name].delete_one(query)
        return result.deleted_count > 0

    async def delete_many(self, collection_name: str, query: Dict[str, Any]) -> int:
        """
        批量删除符合条件的文档

        :param collection_name: 集合名称
        :param query: 查询条件
        :return: 删除的文档数量
        """
        result = await self.db[collection_name].delete_many(query)
        return result.deleted_count

    async def close(self):
        self._client.close()

    async def count_documents(self, collection_name: str, query: Dict[str, Any]) -> int:
        count = await self.db[collection_name].count_documents(query)
        return count

    async def create_index(self, collection_name: str, keys: List[tuple], unique: bool = False):
        """Create an index for the specified collection"""
        return await self.db[collection_name].create_index(keys, unique=unique)

    async def list_indexes(self, collection_name: str):
        """List all indexes in the collection"""
        return await self.db[collection_name].list_indexes().to_list(None)


async def find_with_pagination(
        self,
        collection_name: str,
        query: Dict[str, Any],
        page: int = 1,
        page_size: int = 10,
        sort: List[Tuple[str, int]] = None
) -> Dict[str, Any]:
    """
    Fetch documents with pagination

    Args:
        collection_name: Collection to query
        query: MongoDB query dict
        page: Page number (starts from 1)
        page_size: Number of items per page
        sort: List of tuples with field name and direction [(field_name, direction)]
            direction: 1 for ascending, -1 for descending
    """
    skip = (page - 1) * page_size
    cursor = self.db[collection_name].find(query)

    if sort:
        # Convert list of tuples to list of lists for MongoDB
        mongodb_sort = [[field, direction] for field, direction in sort]
        cursor.sort(mongodb_sort)

    total = await self.db[collection_name].count_documents(query)
    documents = await cursor.skip(skip).limit(page_size).to_list(None)

    return {
        "items": documents,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": ceil(total / page_size)
    }
