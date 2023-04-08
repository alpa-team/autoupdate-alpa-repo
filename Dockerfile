FROM python:3-alpine

WORKDIR /action_app
COPY . .

RUN pip install -r requirements.txt

ENV PYTHONPATH /action_app
CMD ["/action_app/autoupdate/autoupdate.py"]
