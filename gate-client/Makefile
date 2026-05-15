.PHONY: install run clean

venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

install: venv

run:
	export LG_WD=/tmp; .venv/bin/python3 gate_client.py

clean:
	rm -rf .venv
	find . -type d -name "__pycache__" -exec rm -rf {} +