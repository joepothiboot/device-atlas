.PHONY: test docs
test:
	python3 -m unittest discover -s tests
docs:
	python3 -m atlas docs
