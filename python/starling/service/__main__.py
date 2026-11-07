"""`python -m starling.service` 等同于 `starling-serve`。"""
import sys

from starling.service.serve import main

sys.exit(main())
