.PHONY: install test lint run clean

install:
	pip install -r requirements.txt

test:
	pytest

lint:
	flake8

run:
	python app.py

clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -exec rm -rf {} +
