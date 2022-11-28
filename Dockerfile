FROM python:3.9-slim

RUN mkdir app
WORKDIR /app

RUN pip install --upgrade pip

ADD requirements.txt .
RUN pip install -r requirements.txt

COPY . ./

CMD [ "gunicorn", "--workers=5", "--threads=1", "-b 0.0.0.0:8000", "--timeout=600", "app:server"]