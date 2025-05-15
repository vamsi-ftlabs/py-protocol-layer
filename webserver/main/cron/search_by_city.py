import os
import time
import uuid
from datetime import datetime, timedelta

from main.config import get_config_by_name
from main.logger.custom_logging import log_error, log
from main.models import get_mongo_collection
from main.models.catalog import SearchType
from main.repository import mongo
from main.service.common import dump_request_payload, update_dumped_request_with_response
from main.service.search import gateway_search


def make_http_requests_for_search_by_city(search_type: SearchType, domains=None, cities=None, mode="start"):
    log(f"Starting catalog {search_type.value} operation with mode: {mode}")
    search_payload_list = []
    domain_list = get_config_by_name("DOMAIN_LIST") if domains is None else domains
    end_time = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    start_time = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    log(f"Catalog refresh time range: {start_time} to {end_time}")
    payment_object = {
        "@ondc/org/buyer_app_finder_fee_type": get_config_by_name("BAP_FINDER_FEE_TYPE"),
        "@ondc/org/buyer_app_finder_fee_amount": get_config_by_name("BAP_FINDER_FEE_AMOUNT")
    }
    if search_type == SearchType.FULL:
        city_list = get_config_by_name("CITY_LIST") if cities is None else cities
        log(f"Full catalog refresh for domains: {domain_list} and cities: {city_list}")
        message = {
            "intent": {
                "fulfillment":
                    {
                        "type": "Delivery"
                    },
                "payment": payment_object
            }
        }
    else:
        city_list = ["*"] if cities is None else cities
        log(f"Incremental catalog refresh for domains: {domain_list} and cities: {city_list}")
        if mode == "start_and_stop":
            message = {
                "intent":
                    {
                        "payment": payment_object,
                        "tags":
                            [
                                {
                                    "code": "catalog_inc",
                                    "list":
                                        [
                                            {
                                                "code": "start_time",
                                                "value": start_time
                                            },
                                            {
                                                "code": "end_time",
                                                "value": end_time
                                            }
                                        ]
                                }
                            ]
                    }
            }
        else:
            message = {
                "intent": {
                    "payment": payment_object,
                    "tags":
                        [
                            {
                                "code":"catalog_inc",
                                "list":
                                    [
                                        {
                                            "code": "mode",
                                            "value": mode
                                        }
                                    ]
                            }
                        ]
                }
            }

    for d in domain_list:
        for c in city_list:
            if search_type == SearchType.INC and mode == "stop":
                transaction_id = get_transaction_id_of_last_start(d, c)
                if transaction_id is None:
                    log_error(f"Transaction-id not found for start for {d}")
                    continue
            else:
                transaction_id = str(uuid.uuid4())
            search_payload = {
                "context": {
                    "domain": d,
                    "action": "search",
                    "country": "IND",
                    "city": c,
                    "core_version": "1.2.0",
                    "bap_id": get_config_by_name("BAP_ID"),
                    "bap_uri": get_config_by_name("BAP_URL") + "/protocol/v1",
                    "transaction_id": transaction_id,
                    "message_id": str(uuid.uuid4()),
                    "timestamp": end_time,
                    "ttl": "PT30S"
                },
                "message": message
            }
            search_payload_list.append(search_payload)
    
    log(f"Created {len(search_payload_list)} search requests for catalog refresh")

    for index, x in enumerate(search_payload_list):
        log(f"Processing catalog refresh request {index + 1}/{len(search_payload_list)} for domain: {x['context']['domain']}, city: {x['context']['city']}")
        dump_request_and_make_gateway_search(search_type, x)
        time.sleep(1)
    
    log(f"Completed catalog {search_type.value} operation with mode: {mode}")


def get_transaction_id_of_last_start(domain, city):
    log(f"Getting last transaction ID for domain: {domain}, city: {city}")
    search_collection = get_mongo_collection('request_dump')
    query_object = {"action": "search", "request.context.domain": domain, "request.context.city": city,
                    "request.message.intent.tags.list.value": "start"}
    catalog = mongo.collection_find_one_with_sort(search_collection, query_object, "created_at")
    if catalog:
        log(f"Found transaction ID: {catalog['request']['context']['transaction_id']} for domain: {domain}, city: {city}")
    else:
        log_error(f"No transaction ID found for domain: {domain}, city: {city}")
    return catalog['request']['context']['transaction_id'] if catalog else None


def dump_request_and_make_gateway_search(search_type, search_payload):
    log(f"Sending catalog search request for domain: {search_payload['context']['domain']}, transaction_id: {search_payload['context']['transaction_id']}")
    headers = {'X-ONDC-Search-Response': search_type.value}
    entry_object_id = dump_request_payload("search", search_payload)
    resp = gateway_search(search_payload, headers)
    update_dumped_request_with_response(entry_object_id, resp)
    log(f"Completed catalog search request for domain: {search_payload['context']['domain']}, transaction_id: {search_payload['context']['transaction_id']}")


def make_full_catalog_search_requests(domains=None, cities=None):
    log("Starting FULL catalog refresh")
    make_http_requests_for_search_by_city(SearchType.FULL, domains=domains, cities=cities)
    log("Completed FULL catalog refresh")


def make_incremental_catalog_search_requests(domains=None, cities=None, mode="start"):
    log(f"Starting INCREMENTAL catalog refresh with mode: {mode}")
    make_http_requests_for_search_by_city(SearchType.INC, domains, cities, mode)
    log(f"Completed INCREMENTAL catalog refresh with mode: {mode}")


def make_search_operation_along_with_incremental():
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    log(f"=== CATALOG REFRESH STARTED AT {timestamp} ===")
    log("First stopping any existing incremental refresh")
    make_incremental_catalog_search_requests(mode="stop")
    log("Now starting a new incremental refresh")
    make_incremental_catalog_search_requests(mode="start")
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    log(f"=== CATALOG REFRESH COMPLETED AT {timestamp} ===")


def run_cron_for_search_catalog(full_or_inc):
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    log(f'Running cron for {full_or_inc} catalog at {timestamp}')
    if full_or_inc == "full":
        make_full_catalog_search_requests()
    elif full_or_inc == "inc":
        make_search_operation_along_with_incremental()
    else:
        log_error("Full or incr flag is not set correctly!")


if __name__ == '__main__':
    full_or_inc_flag = os.getenv("FULL_OR_INC")
    run_cron_for_search_catalog(full_or_inc_flag)
