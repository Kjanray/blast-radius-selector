IMAGE ?= blast-radius-entry
SAMPLE ?= $(CURDIR)/../kit/sample
OUTPUT := $(CURDIR)/output.jsonl
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro
build:
	docker build --platform linux/amd64 --tag $(IMAGE) .
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python ../kit/score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)
