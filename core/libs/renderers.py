from rest_framework.renderers import JSONRenderer


class CustomRenderer(JSONRenderer):

    def render(self, data, accepted_media_type=None, renderer_context=None):
        status_code = renderer_context['response'].status_code
        response = {
          "status": "success",
          "code": status_code,
          "data": data,
          "message": None
        }

        if not str(status_code).startswith('2'):
            response["status"] = "error"
            response["data"] = None
            try:
                response["message"] = data["detail"]
            except KeyError:
                response["data"] = data

        # if status_code>=400 and data:
        #     data_values = data.values()
        #     for message in data_values:
        #         if isinstance(message, list):
        #             message_list = [message for messages in data_values for message in messages]
        #             response["data"] = None
        #             response["message"] = message_list

        return super(CustomRenderer, self).render(response, accepted_media_type, renderer_context)