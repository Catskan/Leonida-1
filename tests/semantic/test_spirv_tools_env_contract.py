"""Regression coverage for LEONIDA_REQUIRE_SPIRV_TOOLS handling in semantic helpers.

The Windows semantic leg sets LEONIDA_REQUIRE_SPIRV_TOOLS to the string "0".
Python treats a non-empty string as true, so a bare truthy check would demand
spirv-as / spirv-val on a leg where they are optional.  Only the exact string
"1" means the tools are mandatory.

All inputs here are synthetic; no game files are read.
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_active_cache  # noqa: E402
import test_reg_ptr_hoist  # noqa: E402
import test_sopc_validation  # noqa: E402
import test_unary_f16_validation  # noqa: E402


class SpirvToolsEnvContractTests(unittest.TestCase):
    def _helpers(self):
        return (
            ("test_active_cache", test_active_cache._validate_spirv, ("synthetic",)),
            ("test_reg_ptr_hoist", test_reg_ptr_hoist._validate_spirv, ("synthetic",)),
            ("test_unary_f16_validation", test_unary_f16_validation.validate_spirv, ("synthetic",)),
            ("test_sopc_validation", test_sopc_validation._translate_and_validate_cs,
             ("cs_env_contract", ["s_nop 0"], 64)),
        )

    def _check(self, value):
        env = dict(os.environ)
        env.pop("LEONIDA_REQUIRE_SPIRV_TOOLS", None)
        if value is not None:
            env["LEONIDA_REQUIRE_SPIRV_TOOLS"] = value
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch.object(test_sopc_validation.rdna2spirv, "translate",
                                  return_value="synthetic"):
            for name, helper, args in self._helpers():
                if value == "1":
                    with self.assertRaises(AssertionError, msg=name):
                        helper(*args)
                else:
                    # Missing tools are optional: the interpreter checks still run and
                    # no AssertionError is raised.
                    helper(*args)

    def test_unset(self):
        self._check(None)

    def test_zero_string(self):
        self._check("0")

    def test_one_string(self):
        self._check("1")


if __name__ == "__main__":
    unittest.main()
