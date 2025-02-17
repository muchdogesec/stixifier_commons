import contextlib
import logging
import typing, re
from arango import ArangoClient
from django.conf import settings
from rest_framework.response import Response
from drf_spectacular.utils import OpenApiParameter
from ..utils.pagination import Pagination
from rest_framework.exceptions import ValidationError
from stix2arango.services import ArangoDBService
from . import conf

from django.http import HttpResponse
from django.conf import settings
from rest_framework import decorators, response, status


SDO_TYPES = set([
    "attack-pattern",
    "campaign",
    "course-of-action",
    "grouping",
    "identity",
    "incident",
    "indicator",
    "infrastructure",
    "intrusion-set",
    "location",
    "malware",
    "malware-analysis",
    "note",
    "observed-data",
    "opinion",
    "report",
    "threat-actor",
    "sighting",
    "tool",
    "vulnerability",
    "weakness",
])

SCO_TYPES = set([
    "artifact",
    "autonomous-system",
    "bank-account",
    "bank-card",
    "cryptocurrency-transaction",
    "cryptocurrency-wallet",
    "directory",
    "domain-name",
    "email-addr",
    "email-message",
    "file",
    "ipv4-addr",
    "ipv6-addr",
    "mac-addr",
    "mutex",
    "network-traffic",
    "phone-number",
    "process",
    "software",
    "url",
    "user-account",
    "user-agent",
    "windows-registry-key",
    "x509-certificate"
])
SDO_SORT_FIELDS = [
    "name_ascending",
    "name_descending",
    "created_ascending",
    "created_descending",
    "modified_ascending",
    "modified_descending",
    "type_ascending",
    "type_descending"
]
SRO_SORT_FIELDS = [
    "created_ascending",
    "created_descending",
    "modified_ascending",
    "modified_descending",
]


SCO_SORT_FIELDS = [
    "type_ascending",
    "type_descending"
]


SMO_SORT_FIELDS = [
    "created_ascending",
    "created_descending",
    "type_ascending",
    "type_descending",
]



SMO_TYPES = set([
    "marking-definition",
    "extension-definition",
    "language-content",
])

OBJECT_TYPES = SDO_TYPES.union(SCO_TYPES).union(["relationship"]).union(SMO_TYPES)

def positive_int(integer_string, cutoff=None, default=1):
    """
    Cast a string to a strictly positive integer.
    """
    with contextlib.suppress(ValueError, TypeError):
        ret = int(integer_string)
        if ret <= 0:
            return default
        if cutoff:
            return min(ret, cutoff)
        return ret
    return default

class ArangoDBHelper:
    max_page_size = conf.MAXIMUM_PAGE_SIZE
    page_size = conf.DEFAULT_PAGE_SIZE
    SRO_OBJECTS_ONLY_LATEST = getattr(settings, 'SRO_OBJECTS_ONLY_LATEST', True)

    @staticmethod
    def get_like_literal(str: str):
        return str.replace('_', '\\_').replace('%', '\\%')
    def get_sort_stmt(self, fields: list[str]):
        finder = re.compile(r"(.+)_((a|de)sc)ending")
        sort_field = self.query.get('sort', fields[0])
        if sort_field not in fields:
            return ""
        if m := finder.match(sort_field):
            field = m.group(1)
            direction = m.group(2).upper()
            return f"SORT doc.{field} {direction}"

    def query_as_array(self, key):
        query = self.query.get(key)
        if not query:
            return []
        return query.split(',')
    
    def query_as_bool(self, key, default=True):
        query_str = self.query.get(key)
        if not query_str:
            return default
        return query_str.lower() == 'true'

    @classmethod
    def get_page_params(cls, request):
        kwargs = request.GET.copy()
        page_number = positive_int(kwargs.get('page'))
        page_limit = positive_int(kwargs.get('page_size'), cutoff=ArangoDBHelper.max_page_size, default=ArangoDBHelper.page_size)
        return page_number, page_limit

    @classmethod
    def get_paginated_response(cls, data, page_number, page_size=page_size, full_count=0, result_key="objects"):
        return Response(
            {
                "page_size": page_size or cls.page_size,
                "page_number": page_number,
                "page_results_count": len(data),
                "total_results_count": full_count,
                result_key: data,
            }
        )


    @classmethod
    def get_paginated_response_schema(cls, result_key="objects", schema=None):
        return {
            200: {
                "type": "object",
                "required": ["page_results_count", result_key],
                "properties": {
                    "page_size": {
                        "type": "integer",
                        "example": cls.max_page_size,
                    },
                    "page_number": {
                        "type": "integer",
                        "example": 3,
                    },
                    "page_results_count": {
                        "type": "integer",
                        "example": cls.page_size,
                    },
                    "total_results_count": {
                        "type": "integer",
                        "example": cls.page_size * cls.max_page_size,
                    },
                    result_key: {
                        "type": "array",
                        "items": schema or {
                            "type": "object",
                            "properties": {
                                "type":{
                                    "example": "domain-name",
                                },
                                "id": {
                                    "example": "domain-name--a86627d4-285b-5358-b332-4e33f3ec1075",
                                },
                            },
                            "additionalProperties": True,
                        }
                    }
                }
            },
            400: {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string"
                    },
                    "code": {
                        "type": "integer"
                    }
                },
                "required": [
                    "code",
                ]
            }
        }

    @classmethod
    def get_schema_operation_parameters(self):
        parameters = [
            OpenApiParameter(
                "page",
                type=int,
                description=Pagination.page_query_description,
            ),
            OpenApiParameter(
                "page_size",
                type=int,
                description=Pagination.page_size_query_description,
            ),
        ]
        return parameters




    client = ArangoClient(
        hosts=settings.ARANGODB_HOST_URL
    )
    DB_NAME = conf.DB_NAME

    def __init__(self, collection, request, result_key="objects") -> None:
        self.collection = collection
        self.db = self.client.db(
            self.DB_NAME,
            username=settings.ARANGODB_USERNAME,
            password=settings.ARANGODB_PASSWORD,
        )
        self.result_key = result_key
        self.page, self.count = self.get_page_params(request)
        self.request = request
        self.query = request.query_params.dict()

    def execute_query(self, query, bind_vars={}, paginate=True):
        if paginate:
            bind_vars['offset'], bind_vars['count'] = self.get_offset_and_count(self.count, self.page)
        try:
            cursor = self.db.aql.execute(query, bind_vars=bind_vars, count=True, full_count=True)
        except Exception as e:
            logging.exception(e)
            raise ValidationError("aql: cannot process request")
        if paginate:
            return self.get_paginated_response(cursor, self.page, self.page_size, cursor.statistics()["fullCount"], result_key=self.result_key)
        return list(cursor)

    def get_offset_and_count(self, count, page) -> tuple[int, int]:
        page = page or 1
        if page >= 2**32:
            raise ValidationError(f"invalid page `{page}`")
        offset = (page-1)*count
        return offset, count
    
    def get_reports(self, id=None):
        bind_vars = {
                "@collection": self.collection,
                "type": 'report',
        }
        query = """
            FOR doc in @@collection
            FILTER doc.type == @type AND doc._is_latest
            LIMIT @offset, @count
            RETURN KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)
        
    def get_scos(self, matcher={}):
        types = SCO_TYPES
        other_filters = []

        if new_types := self.query_as_array('types'):
            types = types.intersection(new_types)
        bind_vars = {
                "@collection": self.collection,
                "types": list(types),
        }
        if value := self.query.get('value'):
            bind_vars['search_value'] = value.lower()
            other_filters.append(
                """
                (
                    doc.type == 'artifact' AND CONTAINS(LOWER(doc.payload_bin), @search_value) OR
                    doc.type == 'autonomous-system' AND CONTAINS(LOWER(doc.number), @search_value) OR
                    doc.type == 'bank-account' AND CONTAINS(LOWER(doc.iban_number), @search_value) OR
                    doc.type == 'bank-card' AND CONTAINS(LOWER(doc.number), @search_value) OR
                    doc.type == 'cryptocurrency-transaction' AND CONTAINS(LOWER(doc.hash), @search_value) OR
                    doc.type == 'cryptocurrency-wallet' AND CONTAINS(LOWER(doc.hash), @search_value) OR
                    doc.type == 'directory' AND CONTAINS(LOWER(doc.path), @search_value) OR
                    doc.type == 'domain-name' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'email-addr' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'email-message' AND CONTAINS(LOWER(doc.body), @search_value) OR
                    doc.type == 'file' AND CONTAINS(LOWER(doc.name), @search_value) OR
                    doc.type == 'ipv4-addr' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'ipv6-addr' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'mac-addr' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'mutex' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'network-traffic' AND CONTAINS(LOWER(doc.protocols), @search_value) OR
                    doc.type == 'phone-number' AND CONTAINS(LOWER(doc.number), @search_value) OR
                    doc.type == 'process' AND CONTAINS(LOWER(doc.pid), @search_value) OR
                    doc.type == 'software' AND CONTAINS(LOWER(doc.name), @search_value) OR
                    doc.type == 'url' AND CONTAINS(LOWER(doc.value), @search_value) OR
                    doc.type == 'user-account' AND CONTAINS(LOWER(doc.display_name), @search_value) OR
                    doc.type == 'user-agent' AND CONTAINS(LOWER(doc.string), @search_value) OR
                    doc.type == 'windows-registry-key' AND CONTAINS(LOWER(doc.key), @search_value) OR
                    doc.type == 'x509-certificate' AND CONTAINS(LOWER(doc.subject), @search_value)
                    //generic
                    OR
                    CONTAINS(LOWER(doc.value), @search_value) OR
                    CONTAINS(LOWER(doc.name), @search_value) OR
                    CONTAINS(LOWER(doc.number), @search_value)
                )
                """.strip()
            )

        # if post_id := self.query.get('post_id'):
        #     matcher["_obstracts_post_id"] = post_id

        # if report_id := self.query.get('report_id'):
        #     matcher["_stixify_report_id"] = report_id

        if matcher:
            bind_vars['matcher'] = matcher
            other_filters.insert(0, "MATCHES(doc, @matcher)")


        if other_filters:
            other_filters = "FILTER " + " AND ".join(other_filters)

        query = f"""
            FOR doc in @@collection SEARCH doc.type IN @types AND doc._is_latest == TRUE
            {other_filters or ""}
            {self.get_sort_stmt(SCO_SORT_FIELDS)}


            LIMIT @offset, @count
            RETURN KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)

    
    def get_smos(self):
        types = SMO_TYPES
        if new_types := self.query_as_array('types'):
            types = types.intersection(new_types)
        bind_vars = {
            "@collection": self.collection,
            "types": list(types),
        }
        other_filters = {}
        query = f"""
            FOR doc in @@collection
            SEARCH doc.type IN @types AND doc._is_latest == TRUE
            {other_filters or ""}
            {self.get_sort_stmt(SMO_SORT_FIELDS)}


            LIMIT @offset, @count
            RETURN  KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)
    
      
    def get_sdos(self):
        types = SDO_TYPES
        if new_types := self.query_as_array('types'):
            types = types.intersection(new_types)
        

        bind_vars = {
            "@collection": self.collection,
            "types": list(types),
        }
        other_filters = []
        search_filters = ['doc._is_latest == TRUE']
        if term := self.query.get('labels'):
            bind_vars['labels'] = term
            other_filters.append("doc.labels[? ANY FILTER CONTAINS(CURRENT, @labels)]")

        if term := self.query.get('name'):
            bind_vars['name'] = "%" + self.get_like_literal(term) + '%'
            search_filters.append("doc.name LIKE @name")

        if other_filters:
            other_filters = "FILTER " + " AND ".join(other_filters)

        query = f"""
            FOR doc in @@collection
            SEARCH doc.type IN @types AND {' AND '.join(search_filters)}
            {other_filters or ""}
            {self.get_sort_stmt(SDO_SORT_FIELDS)}


            LIMIT @offset, @count
            RETURN  KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)
    
    def get_objects_by_id(self, id):
        bind_vars = {
            "@view": self.collection,
            "id": id,
        }
        query = """
            FOR doc in @@view
            SEARCH doc.id == @id AND doc._is_latest == TRUE
            LET _unused = [@offset, @count]
            LIMIT 1
            RETURN KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)
    
    def get_containing_reports(self, id):
        bind_vars = {
            "@view": self.collection,
            "id": id,
        }
        query = """
            LET report_ids = (
                FOR doc in @@view
                FILTER doc.id == @id
                RETURN DISTINCT doc._stixify_report_id
            )
            FOR report in @@view
            SEARCH report.type == 'report' AND report.id IN report_ids
            LIMIT @offset, @count
            RETURN KEEP(report, KEYS(report, TRUE))
        """
        return self.execute_query(query, bind_vars=bind_vars)
    
    def get_sros(self):
        bind_vars = {
            "@collection": self.collection,
        }

        search_filters = ['doc._is_latest == TRUE']
        
        if terms := self.query_as_array('source_ref_type'):
            bind_vars['source_ref_type'] = terms
            search_filters.append('doc._source_type IN @source_ref_type')
            
        if terms := self.query_as_array('target_ref_type'):
            bind_vars['target_ref_type'] = terms
            search_filters.append('doc._target_type IN @target_ref_type')

        if term := self.query.get('relationship_type'):
            bind_vars['relationship_type'] = '%' + self.get_like_literal(term) + '%'
            search_filters.append("doc.relationship_type LIKE @relationship_type")


        if not self.query_as_bool('include_embedded_refs', True):
            search_filters.append('doc._is_ref != TRUE')

        if term := self.query.get('target_ref'):
            bind_vars['target_ref'] = term
            search_filters.append('doc.target_ref == @target_ref')

        if term := self.query.get('source_ref'):
            bind_vars['source_ref'] = term
            search_filters.append('doc.source_ref == @source_ref')

        if not self.SRO_OBJECTS_ONLY_LATEST:
            search_filters[0] = '(doc._is_latest == TRUE OR doc._target_type IN @sco_types OR doc._source_type IN @sco_types)'
            bind_vars['sco_types'] = list(SCO_TYPES)

        query = f"""
            FOR doc in @@collection
            SEARCH doc.type == 'relationship' AND { ' AND '.join(search_filters) }
            {self.get_sort_stmt(SRO_SORT_FIELDS)}

            LIMIT @offset, @count
            RETURN KEEP(doc, KEYS(doc, true))

        """
        return self.execute_query(query, bind_vars=bind_vars)
    
    
    def get_post_objects(self, post_id, feed_id):
        types = self.query.get('types', "")
        bind_vars = {
            "@view": self.collection,
            "matcher": dict(_obstracts_post_id=str(post_id), _obstracts_feed_id=str(feed_id)),
            "types": list(OBJECT_TYPES.intersection(types.split(","))) if types else None,
        }
        query = """
            FOR doc in @@view
            FILTER doc.type IN @types OR NOT @types
            FILTER MATCHES(doc, @matcher)

            COLLECT id = doc.id INTO docs
            LET doc = FIRST(FOR d in docs[*].doc SORT d.modified OR d.created DESC RETURN d)

            LIMIT @offset, @count
            RETURN KEEP(doc, KEYS(doc, true))
        """
        return self.execute_query(query, bind_vars=bind_vars)
    

    def delete_report_object(self, report_id, object_id):
        db_service = ArangoDBService(
            self.DB_NAME,
            [],
            [],
            create=False,
            username=settings.ARANGODB_USERNAME,
            password=settings.ARANGODB_PASSWORD,
            host_url=settings.ARANGODB_HOST_URL,
        )
        query = """
        let doc_ids = (
            FOR doc IN @@view
            SEARCH doc.id IN [@object_id, @report_id] AND doc._stixify_report_id == @report_id
            SORT doc.object_refs
            RETURN [doc._id, doc.id]
        )
        LET doc_id = FIRST(doc_ids[* FILTER CURRENT[1]== @object_id])
        LET report_id = FIRST(FIRST(doc_ids[* FILTER CURRENT[1] == @report_id]))

        RETURN [report_id, doc_id, (FOR d IN APPEND([doc_id], (
                FOR doc IN @@view
                SEARCH doc._from == doc_id[0] OR doc._to == doc_id[0]
                RETURN [doc._id, doc.id])
            )
        FILTER d != NULL
        RETURN d)]
        """
        bind_vars = {
            "@view": self.collection,
            "object_id": object_id,
            "report_id": report_id,
        }
        report_idkey, doc_id, ids_to_be_removed = self.execute_query(query, bind_vars=bind_vars, paginate=False)[0]
        # separate into collections
        collections = {}
        bind_vars = {
            'ckeys': {}
        }
        queries = []
        stix_ids = []
        for key_id, stix_id in ids_to_be_removed:
            stix_ids.append(stix_id)
            collection_name, _key = key_id.split('/', 1)
            ckeys = collections.setdefault(collection_name, [])
            if not ckeys:
                bind_vars["ckeys"][collection_name] = ckeys
                bind_vars['@'+collection_name] = collection_name
                queries.append(f"(FOR _key IN @ckeys.{collection_name} REMOVE {{_key}} IN @@{collection_name} RETURN TRUE)")
            ckeys.append(_key)
        
        if not queries:
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        queries = ",\n\t".join(queries)
        query = f"""
        RETURN LENGTH(UNION([], [], {queries}))
        """
        resp = self.execute_query(query, bind_vars=bind_vars, paginate=False)
        logging.info(f"{resp} objects removed")
        resp = self.execute_query("""
                                FOR doc in @@collection FILTER doc._id == @report_idkey
                                    UPDATE {_key: doc._key} WITH {object_refs: REMOVE_VALUES(doc.object_refs, @stix_ids)} IN @@collection
                                    RETURN {new_length: LENGTH(NEW.object_refs), old_length: LENGTH(doc.object_refs)}
                                  """, bind_vars={'report_idkey': report_idkey, 'stix_ids': stix_ids, '@collection': report_idkey.split('/')[0]}, paginate=False)
        logging.info(f"removed references from report.object_refs: {resp}")
        doc_collection_name = doc_id.split('/')[0]
        db_service.update_is_latest_several_chunked([object_id], doc_collection_name, doc_collection_name.removesuffix('_vertex_collection')+'_edge_collection')
        return response.Response(status=status.HTTP_204_NO_CONTENT)
        # self.execute_query("LET" (",\n\t".join(queries)), bind_vars=bind_vars)


