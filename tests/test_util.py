"""日志/摘要里 URL 打码的测试：这些参数等同于拉流凭证。"""

from __future__ import annotations

import unittest

from live_capture.util import redact_url


class RedactTests(unittest.TestCase):
    URL = (
        "http://pull-l1.wxlivecdn.com/x/y.m3u8?cdntagname=wxtv1080f&combuf=SECRET1"
        "&extbuf=SECRET2&gid=14438019&openid=SECRET3&token=SECRET4&txSecret=SECRET5"
        "&txTime=6AFE5A27&wxtoken=SECRET6&txspiseq=SECRET7"
    )

    def test_sensitive_params_masked(self):
        out = redact_url(self.URL)
        for secret in ("SECRET1", "SECRET2", "SECRET3", "SECRET4", "SECRET5", "SECRET6", "SECRET7"):
            self.assertNotIn(secret, out)
        # 不影响可读性：域名、路径、非敏感参数保留
        self.assertIn("pull-l1.wxlivecdn.com", out)
        self.assertIn("cdntagname=wxtv1080f", out)
        self.assertIn("gid=14438019", out)
        self.assertIn("txTime=6AFE5A27", out)


if __name__ == "__main__":
    unittest.main()
