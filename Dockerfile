FROM fedora:latest

WORKDIR /action_app
COPY . .

RUN dnf install -y git pip
RUN pip install -r requirements.txt

ENV PYTHONPATH /action_app
CMD ["python3", "/action_app/autoupdate/autoupdate.py"]
