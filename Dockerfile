FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libmagic-dev

RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app

ENV PYTHONUNBUFFERED 1

COPY . .

RUN apt-get update \
  && apt-get -y install gcc \
  && apt-get clean \
  && pip install --upgrade pip \
  && pip install -r requirements/stage.txt

ENTRYPOINT ["/usr/src/app/entrypoint.sh"]
