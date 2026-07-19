#!/usr/bin/env python3

from config import load_config
from service import run


def main():
    run(load_config())


if __name__ == "__main__":
    main()
