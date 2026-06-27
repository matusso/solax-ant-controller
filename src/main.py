#!/usr/bin/env python3
"""Compatibility wrapper for running the controller from the source tree."""

from solax_ant_controller.controller import main


if __name__ == "__main__":
    raise SystemExit(main())
