"""
    Elasticsearch Document Object Model for MetaKG
"""
from elasticsearch_dsl import InnerDoc, Keyword, MetaField, Object, Text, analysis, mapping

from .base import BaseDoc

ES_INDEX_NAME = "smartapi_metakg_docs"


# Define some reusable fields and mappings
lowercase_keyword = Keyword(normalizer=analysis.normalizer("lowercase_normalizer", filter=["lowercase"]))
lowercase_keyword_copy_to_all = Keyword(
    normalizer=analysis.normalizer("lowercase_normalizer", filter=["lowercase"]), copy_to="all"
)
lowercase_keyword_node = Keyword(
    normalizer=analysis.normalizer("lowercase_normalizer", filter=["lowercase"]), copy_to=["all", "node"]
)
default_text = Text(fields={"raw": lowercase_keyword}, copy_to="all")
metakg_mapping = mapping.Mapping()
metakg_mapping.meta(
    "dynamic_templates",
    [
        {
            "default_string": {
                "match_mapping_type": "string",
                "mapping": {
                    "type": "text",
                    "fields": {"raw": {"type": "keyword", "ignore_above": 512}},
                    "copy_to": "all",
                },
            }
        }
    ],
)
# add two copy_to fields
metakg_mapping.field("all", "text")  # the default all field for unfielded queries
metakg_mapping.field("node", lowercase_keyword)  # a field combines both subject and object fields


class SmartAPIInnerDoc(InnerDoc):
    id = Keyword(required=True)
    metadata = Text(index=False)
    ui = Text(index=False)


# class TranslatorInnerDoc(InnerDoc):
#     component = lowercase_keyword_copy_to_all
#     team = Text(multi=True, fields={"raw": lowercase_keyword})


class APIInnerDoc(InnerDoc):
    name = default_text
    smartapi = Object(SmartAPIInnerDoc)
    # We cannot define "x-translator" field here due the "-" in the name,
    # so we will have it indexed via the dynamic templates


class MetaKGDoc(BaseDoc):
    subject = lowercase_keyword_node
    object = lowercase_keyword_node
    predicate = lowercase_keyword_copy_to_all
    provided_by = default_text
    api = Object(APIInnerDoc)

    class Index:
        """
        Index Settings
        """

        name = ES_INDEX_NAME
        settings = {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "mapping.ignore_malformed": True,
            "mapping.total_fields.limit": 2500,
        }

    class Meta:
        mapping = metakg_mapping

    def get_url(self):
        return self.api.smartapi.metadata
