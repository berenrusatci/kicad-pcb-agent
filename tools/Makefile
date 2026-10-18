# pcb-agent tools — one entrypoint for everything.
#
# pytest is NOT required: the suite is unittest-based.

PYTHON ?= python3
TOOLS  := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
REPO   := $(dir $(TOOLS))

export PYTHONPATH := $(TOOLS):$(TOOLS)/scripts:$(TOOLS)/scripts/board:$(TOOLS)/scripts/sch:$(TOOLS)/servers

.PHONY: help test test-fast test-pytest registry registry-check selftest clean lint contracts skills

help:
	@echo "make test           run the whole suite (unittest, no pytest needed)"
	@echo "make test-fast      skip the slow zdiff sweep tests"
	@echo "make test-pytest    run via pytest instead (optional dep)"
	@echo "make selftest       run the tools' built-in self-tests"
	@echo "make registry       regenerate config/tool_registry.json + re-pin fixture"
	@echo "make registry-check verify the registry matches disk (exit 1 on drift)"
	@echo "make skills         materialize pcb-agent/skills into .claude/skills"
	@echo "make contracts      GUVENLIK KILIDI: semasi/testi olmayan arac registry'e giremez"
	@echo "make lint           byte-compile every module"
	@echo "make clean          remove __pycache__"

test:
	@cd $(REPO) && $(PYTHON) -m unittest discover -s $(TOOLS)/tests -t $(TOOLS) -p 'test_*.py'

test-fast:
	@cd $(REPO) && $(PYTHON) -m unittest discover -s $(TOOLS)/tests -t $(TOOLS) \
		-p 'test_*.py' -k 'not sweep and not Monotonicity'

test-pytest:
	@cd $(REPO) && $(PYTHON) -m pytest $(TOOLS)/tests

# Every tool that HAS a --self-test runs it. Discovered, not listed: a
# hand-maintained list silently stops covering tools as they are added, which
# is how the previous two-entry version ended up missing 27 of them.
selftest:
	@cd $(TOOLS) && for f in $$(grep -rl --include='*.py' \
		--exclude-dir=__pycache__ -e '--self-test' scripts | sort); do \
		printf '%s ... ' "$$f"; \
		if $(PYTHON) "$$f" --self-test >/dev/null 2>&1; then echo PASS; \
		else echo FAIL; $(PYTHON) "$$f" --self-test 2>&1 | tail -5; exit 1; fi; \
	done
	@echo "all self-tests PASS"

registry:
	@cd $(REPO) && $(PYTHON) $(TOOLS)/scripts/sync_tool_registry.py --write

registry-check:
	@cd $(REPO) && $(PYTHON) $(TOOLS)/scripts/sync_tool_registry.py --check

lint:
	@$(PYTHON) -m compileall -q $(TOOLS)/common $(TOOLS)/scripts $(TOOLS)/servers && echo "compile OK"

clean:
	@find $(TOOLS) -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"

# --------------------------------------------------------------------------- #
# SOZLESME KILIDI (2026-08-11, Sutun 3)
#
# `registry-check` kaydin DISKLE tuttugunu sinar; bu hedef kaydin ANLAMLI
# oldugunu sinar: odak kategorilerdeki her aracin COZULEBILIR bir input_schema
# ve GERCEKTEN VAR OLAN bir testi olmali. Ikisi ayri sorudur ve ikisi de
# gereklidir - kayitli ama sozlesmesiz bir arac, ajan icin cagrilamaz bir
# aractir.
# --------------------------------------------------------------------------- #
skills:
	@$(PYTHON) $(TOOLS)/scripts/sync_skills.py

contracts:
	@$(PYTHON) $(TOOLS)/tests/test_skill_contracts.py
