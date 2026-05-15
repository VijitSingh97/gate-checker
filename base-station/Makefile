.PHONY: install run clean

venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

install: venv

run:
	.venv/bin/python3 base_station.py

clean:
	rm -rf .venv
	find . -type d -name "__pycache__" -exec rm -rf {} +