# On Windows without make, run the python commands directly (see README).
.PHONY: install test portal demo
install:
	python -m pip install -r requirements.txt && python -m pip install -e .
test:
	python -m pytest
portal:
	python -m uvicorn mock_company.portal.app:app --port 8000
demo:
	python scripts/foundation_demo.py
