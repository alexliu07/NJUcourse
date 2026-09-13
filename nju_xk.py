# -*- coding: utf-8 -*-
"""
南京大学选课系统 核心库
- 与网页完全一致的 DES(登录密码) / AES(选课参数) 加密
- 登录 / 课程列表 / 选课 / 轮询 客户端
"""
import base64
import json
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ============ 常量 ============
BASE = "https://xk.nju.edu.cn/xsxkapp"

# 登录密码 DES 三密钥（des.min.js getDesKeys()）
DES_KEYS = ("this", "password", "is")

# 选课参数 AES-128-ECB 密钥（crypto_aesjs.min.js 全局变量 avy）
AES_KEY = b"wHm1xj3afURghi0c"

# 分类菜单 -> 课程列表接口
COURSE_ENDPOINT = {
    "ZY": "/sys/xsxkapp/elective/programCourse.do",   # 专业
    "QB": "/sys/xsxkapp/elective/queryCourse.do",     # 课表查询
    "SC": "/sys/xsxkapp/elective/queryfavorite.do",   # 收藏
    # 其余(公共/跨专业/通修/体育/导学等) 都走 publicCourse.do
}


# ============ DES（与 des.min.js 逐位一致，已验证） ============

def _str_to_bt(s):
    """每个字符转 16 位（不足 4 字符补 0），返回 64 位列表"""
    a = [0] * 64
    n = min(len(s), 4)
    for i in range(n):
        c = ord(s[i]) & 0xFFFF
        for t in range(16):
            a[16 * i + t] = (c >> (15 - t)) & 1
    return a


def _get_key_bytes(key):
    e = []
    n = len(key) // 4
    t = len(key) % 4
    for s in range(n):
        e.append(_str_to_bt(key[4 * s:4 * s + 4]))
    if t > 0:
        e.append(_str_to_bt(key[4 * n:]))
    return e


def _generate_keys(key_bits):
    e = [0] * 56
    shifts = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
    for t in range(7):
        for j in range(8):
            e[8 * t + j] = key_bits[8 * (7 - j) + t]
    pc2 = [13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3,
           25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36, 46, 54, 29, 39,
           50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31]
    keys = []
    for t in range(16):
        for _ in range(shifts[t]):
            s0 = e[0]
            s28 = e[28]
            for k in range(27):
                e[k] = e[k + 1]
                e[28 + k] = e[29 + k]
            e[27] = s0
            e[55] = s28
        c = [0] * 48
        for m in range(48):
            c[m] = e[pc2[m]]
        keys.append(c)
    return keys


def _init_permute(r):
    e = [0] * 64
    m, n = 1, 0
    for i in range(4):
        for j in range(7, -1, -1):
            k = 7 - j
            e[8 * i + k] = r[8 * j + m]
            e[8 * i + k + 32] = r[8 * j + n]
        m += 2
        n += 2
    return e


def _expand_permute(r):
    e = [0] * 48
    for i in range(8):
        e[6 * i + 0] = r[31] if i == 0 else r[4 * i - 1]
        e[6 * i + 1] = r[4 * i + 0]
        e[6 * i + 2] = r[4 * i + 1]
        e[6 * i + 3] = r[4 * i + 2]
        e[6 * i + 4] = r[4 * i + 3]
        e[6 * i + 5] = r[0] if i == 7 else r[4 * i + 4]
    return e


def _xor(a, b):
    return [a[i] ^ b[i] for i in range(len(a))]


_SBOX = [
    [[14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7],
     [0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8],
     [4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0],
     [15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13]],
    [[15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10],
     [3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5],
     [0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15],
     [13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9]],
    [[10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8],
     [13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1],
     [13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7],
     [1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12]],
    [[7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15],
     [13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9],
     [10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4],
     [3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14]],
    [[2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9],
     [14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6],
     [4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14],
     [11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3]],
    [[12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11],
     [10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8],
     [9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6],
     [4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13]],
    [[4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1],
     [13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6],
     [1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2],
     [6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12]],
    [[13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7],
     [1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2],
     [7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8],
     [2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]],
]


def _sbox_permute(r):
    e = [0] * 32
    for m in range(8):
        l = 2 * r[6 * m + 0] + r[6 * m + 5]
        b = (r[6 * m + 1] * 8 + r[6 * m + 2] * 4 + r[6 * m + 3] * 2 + r[6 * m + 4])
        v = _SBOX[m][l][b]
        e[4 * m + 0] = (v >> 3) & 1
        e[4 * m + 1] = (v >> 2) & 1
        e[4 * m + 2] = (v >> 1) & 1
        e[4 * m + 3] = v & 1
    return e


def _p_permute(r):
    p = [15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9,
         1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24]
    return [r[p[i]] for i in range(32)]


def _finally_permute(r):
    fp = [39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
          37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28,
          35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26,
          33, 1, 41, 9, 49, 17, 57, 25, 32, 0, 40, 8, 48, 16, 56, 24]
    return [r[fp[i]] for i in range(64)]


def _des_enc(block, key):
    keys = _generate_keys(key)
    n = _init_permute(block)
    t = n[:32]
    s = n[32:]
    for c in range(16):
        o = t[:]
        t = s[:]
        s = _xor(_p_permute(_sbox_permute(_xor(_expand_permute(s), keys[c][:]))), o)
    k = [0] * 64
    for c in range(32):
        k[c] = s[c]
        k[32 + c] = t[c]
    return _finally_permute(k)


_HEX = "0123456789ABCDEF"


def _bt64_to_hex(r):
    e = ""
    for i in range(16):
        v = 0
        for j in range(4):
            v = (v << 1) | r[4 * i + j]
        e += _HEX[v]
    return e


def _str_enc(data, k1, k2, k3):
    kb1 = _get_key_bytes(k1)
    kb2 = _get_key_bytes(k2)
    kb3 = _get_key_bytes(k3)
    u = ""
    n = len(data)
    if n == 0:
        return u
    k = n // 4
    y = n % 4
    for v in range(k):
        w = _str_to_bt(data[4 * v:4 * v + 4])
        for kb in kb1:
            w = _des_enc(w, kb)
        for kb in kb2:
            w = _des_enc(w, kb)
        for kb in kb3:
            w = _des_enc(w, kb)
        u += _bt64_to_hex(w)
    if y > 0:
        w = _str_to_bt(data[4 * k:])
        for kb in kb1:
            w = _des_enc(w, kb)
        for kb in kb2:
            w = _des_enc(w, kb)
        for kb in kb3:
            w = _des_enc(w, kb)
        u += _bt64_to_hex(w)
    return u


def encrypt_password(pwd: str) -> str:
    """登录密码加密：DES(strEnc) -> 大写 hex -> base64"""
    return base64.b64encode(_str_enc(pwd, *DES_KEYS).encode("ascii")).decode("ascii")


def aes_encrypt(plain_str: str) -> str:
    """选课参数加密：明文 + '?timestrap=<毫秒时间戳>' 后 AES-128-ECB/PKCS7 -> base64"""
    ms = int(time.time() * 1000)
    data = (plain_str + "?timestrap=" + str(ms)).encode("utf-8")
    ct = AES.new(AES_KEY, AES.MODE_ECB).encrypt(pad(data, 16))
    return base64.b64encode(ct).decode("ascii")


# ============ HTTP 客户端 ============

class XkError(RuntimeError):
    pass


class NJUXKClient:
    def __init__(self, token=None, weu=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        })
        self.token = token
        self.student = {}
        self.batch = {}
        self.menus = []
        self._warmed = False
        if weu:
            self.session.cookies.set("_WEU", weu, domain="xk.nju.edu.cn", path="/xsxkapp")

    # ---- 基础 ----
    @property
    def student_code(self):
        return self.student.get("code")

    @property
    def batch_code(self):
        return self.batch.get("code")

    def _headers(self):
        h = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "language": "zh_cn",
        }
        if self.token:
            h["token"] = self.token
        return h

    def _post(self, path, data=None):
        url = BASE + path
        try:
            r = self.session.post(url, data=data or {}, headers=self._headers(), timeout=15)
        except requests.RequestException as e:
            raise XkError(f"网络请求失败: {e}")
        try:
            return r.json()
        except ValueError:
            raise XkError(f"响应非 JSON (HTTP {r.status_code}): {r.text[:200]}")

    def _warmup(self):
        """首次访问首页，拿到 route / JSESSIONID（幂等）"""
        if self._warmed:
            return
        try:
            self.session.get(BASE + "/sys/xsxkapp/*default/index.do", timeout=15)
        except requests.RequestException:
            pass
        self._warmed = True

    def ensure_waf_cookie(self):
        """elective 系列接口会被 WAF 拦截(502)，必须先持有 _WEU cookie。
        _WEU 由 login.do 的响应下发（即使登录失败也会下发）。"""
        if "_WEU" in self.session.cookies:
            return
        self._warmup()
        try:
            vcode = self.get_vcode()
            self._post("/sys/xsxkapp/student/check/login.do", {
                "loginName": "0",
                "loginPwd": encrypt_password("0"),
                "verifyCode": "1-1,2-2,3-3,4-4",
                "vtoken": "null",
                "uuid": vcode.get("uuid"),
            })
        except Exception:
            pass
        if "_WEU" not in self.session.cookies:
            raise XkError("无法获取 WAF 校验 cookie(_WEU)，请用 --weu 手动传入")

    # ---- 登录 ----
    def get_vcode(self):
        """获取验证码图片与 uuid"""
        self._warmup()
        j = self._post("/sys/xsxkapp/student/4/vcode.do", {})
        if str(j.get("code")) != "1":
            raise XkError(f"获取验证码失败: {j}")
        d = j.get("data") or {}
        return {"uuid": d.get("uuid"), "vtoken": d.get("token"), "image": d.get("vode")}

    def login(self, login_name, password, verify_points, uuid, vtoken, batch_code=None):
        """verify_points: [(x,y), ...] 共 4 个点"""
        if len(verify_points) != 4:
            raise XkError("验证码必须点满 4 个点")
        verify_code = ",".join(f"{int(x)}-{int(y)}" for x, y in verify_points)
        j = self._post("/sys/xsxkapp/student/check/login.do", {
            "loginName": login_name,
            "loginPwd": encrypt_password(password),
            "verifyCode": verify_code,
            "vtoken": vtoken if vtoken is not None else "null",
            "uuid": uuid,
        })
        code = str(j.get("code"))
        if code == "2":
            raise XkError("账号或密码错误")
        if code == "3":
            raise XkError("验证码错误")
        if code == "6":
            raise XkError("验证码已过期，请重试")
        if code == "4":
            raise XkError("在线人数超过上限，请稍后再试")
        if code != "1":
            raise XkError(f"登录失败 code={code}: {j.get('msg') or j}")
        data = j.get("data") or {}
        self.token = data.get("token")
        number = data.get("number")
        # 拉取学籍信息与选课轮次
        j2 = self._post(f"/sys/xsxkapp/student/{number}.do", {})
        if str(j2.get("code")) != "1":
            raise XkError(f"获取学籍信息失败: {j2}")
        self.student = j2.get("data") or {}
        self._pick_batch(batch_code)
        return self.student

    def _pick_batch(self, batch_code=None):
        batches = self.student.get("electiveBatchList") or []
        if not batches:
            raise XkError("当前无可用的选课轮次")
        b = None
        if batch_code:
            for x in batches:
                if str(x.get("code")) == batch_code:
                    b = x
                    break
            if b is None:
                raise XkError(f"选课轮次 {batch_code} 不存在")
        else:
            # 优先选「可选的轮次」：active=1 且 canSelect=1
            for x in batches:
                if str(x.get("active")) == "1" and str(x.get("canSelect")) == "1":
                    b = x
                    break
            if b is None:
                for x in batches:
                    if str(x.get("active")) == "1":
                        b = x
                        break
            if b is None:
                b = batches[0]
        self.batch = b
        self.menus = b.get("limitMenuList") or []
        return b

    def load_student_info(self, student_code, batch_code=None):
        """用已有 token 加载学籍信息与选课轮次（--token 免登录时使用）"""
        self.ensure_waf_cookie()
        j = self._post(f"/sys/xsxkapp/student/{student_code}.do", {})
        if str(j.get("code")) != "1":
            raise XkError(f"获取学籍信息失败: {j.get('msg') or j}")
        self.student = j.get("data") or {}
        self._pick_batch(batch_code)
        return self.student

    # ---- 菜单 ----
    def menu_map(self):
        """返回 {menuCode: 菜单信息}，含父子关系"""
        return {m.get("menuCode"): m for m in self.menus}

    def find_menu(self, code):
        m = self.menu_map().get(code)
        if not m:
            raise XkError(f"菜单 {code} 不存在，可用: {sorted(self.menu_map())}")
        return m

    def course_kind_of(self, menu_code):
        m = self.find_menu(menu_code)
        return str(m.get("courseKind") or "")

    # ---- 课程列表 ----
    def list_courses(self, menu_code, page_number=0, page_size=10, query_content=""):
        self.ensure_waf_cookie()
        qs = {
            "data": {
                "studentCode": self.student_code,
                "electiveBatchCode": self.batch_code,
                "teachingClassType": menu_code,
                "checkConflict": "2",
                "checkCapacity": "2",
                "queryContent": query_content,
            },
            "pageSize": str(page_size),
            "pageNumber": str(page_number),
            "order": "isChoose -",
        }
        endpoint = COURSE_ENDPOINT.get(menu_code, "/sys/xsxkapp/elective/publicCourse.do")
        j = self._post(endpoint, {"querySetting": json.dumps(qs, ensure_ascii=False, separators=(",", ":"))})
        if str(j.get("code")) != "1":
            raise XkError(f"查询课程失败: {j.get('msg') or j}")
        return j.get("dataList") or []

    def search_courses(self, menu_code, keyword, page_size=50):
        """按课程名/课程号/教师 搜索"""
        return self.list_courses(menu_code, page_number=0, page_size=page_size, query_content=keyword)

    # ---- 选课 ----
    def select_course(self, teaching_class_id, menu_code, course_kind=None, operation_type="1"):
        """提交选课（志愿）。返回 volunteer.do 的响应"""
        self.ensure_waf_cookie()
        if course_kind is None:
            course_kind = self.course_kind_of(menu_code)
        add_param = json.dumps({"data": {
            "operationType": operation_type,
            "studentCode": self.student_code,
            "electiveBatchCode": self.batch_code,
            "teachingClassId": teaching_class_id,
            "courseKind": course_kind,
            "teachingClassType": menu_code,
        }}, ensure_ascii=False, separators=(",", ":"))
        encrypted = aes_encrypt(add_param)
        return self._post("/sys/xsxkapp/elective/volunteer.do", {
            "addParam": encrypted,
            "studentCode": self.student_code,
        })

    def poll(self, teaching_class_id, operation_type="1", attempts=12, interval=1.0):
        """轮询处理结果。返回最终响应，超时返回 None"""
        self.ensure_waf_cookie()
        for _ in range(attempts):
            j = self._post("/sys/xsxkapp/elective/studentstatus.do", {
                "studentCode": self.student_code,
                "teachingClassId": teaching_class_id,
                "type": operation_type,
            })
            code = str(j.get("code"))
            if code in ("1", "-1"):
                return j
            time.sleep(interval)
        return None

    def keep_alive(self):
        """轻量请求，尝试保持会话活跃（登录 token 有有效期限）"""
        return self._post("/sys/xsxkapp/student/xkxf.do", {
            "xh": self.student_code,
            "xklcdm": self.batch_code,
        })

    def grab(self, teaching_class_id, menu_code, course_kind=None, poll_attempts=12):
        """选课 + 轮询，返回 (submit_resp, poll_resp)"""
        submit = self.select_course(teaching_class_id, menu_code, course_kind)
        code = str(submit.get("code"))
        if code == "302":
            raise XkError("登录已失效(302)，请重新登录获取 token")
        if code != "1":
            raise XkError(f"选课提交失败 code={code}: {submit.get('msg') or submit}")
        polled = self.poll(teaching_class_id, "1", poll_attempts)
        return submit, polled
