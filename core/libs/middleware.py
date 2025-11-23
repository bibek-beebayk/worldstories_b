import logging
import time
from datetime import datetime
logger = logging.getLogger(__name__)

class APILogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        start_time = time.time()
        response = self.get_response(request)
        duration = time.time() - start_time
        response_ms = duration * 1000
        # response_data = response.data
        response_data = getattr(response, "data", "")
        user = str(getattr(request, 'user', ''))
        method = str(getattr(request, 'method', '')).upper()
        status_code = str(getattr(response, 'status_code', ''))
        token = str(getattr(request, "Authorization", ""))

        logger.info("*"*80)
        logger.info(datetime.strftime(datetime.now(), "%Y-%m-%d  %H:%M:%S"))
        logger.info(f"Request Path: {request.path}")
        logger.info(f"Request Method: {method}")
        logger.info(f"Response Time: {str(response_ms)} ms")
        logger.info(f"User: {user}")
        logger.info(f"Status Code: {status_code}")
        logger.info(f"Token: {token}")
        logger.info(f"Response Data: {response_data}")
        logger.info("*"*80)

        return response