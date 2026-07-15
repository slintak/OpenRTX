# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

STOCK ?= dsp_firmware.bin
OUTPUT ?= dsp_firmware_48khz.bin
PATCHER := patch_dsp_firmware.py
STOCK_URL := https://raw.githubusercontent.com/Tunas1337/lsf-zephyr-sdk-c62/master/resources/dsp_firmware.bin

.DEFAULT_GOAL := all

.PHONY: all patch download help clean

all: patch
	@echo
	@echo "The patched DSP image is ready: $(OUTPUT)"
	@echo "Read README.md before flashing it to the C62 DSP partition."

download: $(STOCK)

$(STOCK):
	@echo "Downloading the stock C62 DSP firmware..."
	curl --fail --location --output $@ $(STOCK_URL)

patch: $(STOCK) $(PATCHER)
	./$(PATCHER) $(STOCK) $(OUTPUT)

help:
	@echo "C62 native-48-kHz DSP firmware helper"
	@echo
	@echo "  make                         download and patch the known stock blob"
	@echo "  make STOCK=/path/to/blob     patch an existing stock blob"
	@echo "  make clean                   remove locally generated images"
	@echo
	@echo "The patcher verifies both input and output SHA-256 hashes."

clean:
	rm -f dsp_firmware.bin dsp_firmware_48khz.bin
