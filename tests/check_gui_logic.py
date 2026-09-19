"""gui.py / grab.py 逻辑自检脚本（不联网、不需要账号）。

在真 Tk 窗口里跑真实界面代码，抢课客户端用假桩替换，验证:
  1. 未登录时抢课被拦下
  2. 重复点「开始循环抢课」不会产生两个并发循环
  3. 「停止循环」能停下来
  4. 定时格式错误 / 间隔为 0 会被拒绝而不是把界面搞崩
  5. 验证码弹窗解析失败时不留残留窗口和输入抓取（grab）
  6. 后台线程写日志是安全的（队列 + 主线程消费）
  7. 收藏夹抢课走成功路径：写回收藏夹、刷新界面、线程回收
  8. 重复收藏项只删掉真正抢成功的那条
  9. favorites.json / session.json 原子写：替换失败时旧文件不被打坏

用法（无桌面环境需要 xvfb）:
    python tests/check_gui_logic.py
    xvfb-run -a python tests/check_gui_logic.py
退出码 0 = 全部通过。
"""
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
except ImportError:
    print("SKIP: 未安装 tkinter（Linux 上需要 python3-tk）")
    sys.exit(0)

import grab
import gui

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


try:
    root = tk.Tk()
except tk.TclError as e:
    print(f"SKIP: 没有可用显示（{e}），请用 xvfb-run 运行")
    sys.exit(0)

# 拦掉所有弹窗，避免测试被模态对话框卡住
dialogs = []
gui.messagebox.showerror = lambda title, msg=None, **k: dialogs.append(("error", title, msg))
gui.messagebox.showinfo = lambda title, msg=None, **k: dialogs.append(("info", title, msg))


class FakeClient:
    """假客户端：不发任何网络请求"""

    def __init__(self, submit_code="1", poll_code="-1"):
        self.token = "FAKE"
        self.student = {"name": "假学生", "code": "000000000"}
        self.batch = {"name": "假轮次", "code": "FAKE"}
        self.menus = [{"menuCode": "GG01", "menuName": "公共", "courseKind": "1"}]
        self.submit_code = submit_code
        self.poll_code = poll_code
        self.submits = 0
        self._lk = threading.Lock()

    @property
    def student_code(self):
        return self.student.get("code")

    def clone_for_thread(self):
        return self

    def course_kind_of(self, menu_code):
        return "1"

    def select_course(self, tid, menu, kind=None):
        with self._lk:
            self.submits += 1
        return {"code": self.submit_code, "msg": "fake"}

    def poll(self, tid, *a, **k):
        return {"code": self.poll_code, "msg": "fake-poll"} if self.poll_code else None

    def keep_alive(self, *a, **k):
        return {}

    def grab(self, tid, menu, kind=None, poll_attempts=12):
        return self.select_course(tid, menu, kind), self.poll(tid)


app = gui.App(root)
steps = []


def loop_threads():
    """当前存活的、来自 gui.py 的后台工作线程"""
    out = []
    for t in threading.enumerate():
        code = getattr(getattr(t, "_target", None), "__code__", None)
        if code is not None and code.co_filename.endswith("gui.py") \
                and code.co_name in ("run", "runner", "worker", "run_parallel") and t.is_alive():
            out.append(t)
    return out


def schedule(delay, fn):
    root.after(int(delay * 1000), fn)


# ---------- 1) 未登录 ----------
def phase1():
    app.grab_menu_var.set("GG01")
    app.grab_tid_var.set("T1")
    app.grab_once()
    check("未登录时抢课被拦下", dialogs and dialogs[-1][0] == "error", str(dialogs[-1:] if dialogs else ""))
    check("未登录时没有起线程", not loop_threads())

    app.client = FakeClient()
    app.grab_interval_var.set("0.05")
    app.grab_retry_var.set("0")
    app.grab_at_var.set("")
    app.start_watch()
    schedule(0.4, phase2)


def phase2():
    app._state = {"loops": len(loop_threads()), "submits": app.client.submits}
    check("第一次点击后只有一个循环在跑", app._state["loops"] == 1, f"loops={app._state['loops']}")

    n_info = len([d for d in dialogs if d[0] == "info"])
    app.start_watch()          # 第二次点击：必须被拦下
    n_new = len([d for d in dialogs if d[0] == "info"]) - n_info
    schedule(0.5, lambda: phase3(n_new))


def phase3(n_new):
    subs = app.client.submits
    check("第二次点击被提示挡下", n_new == 1, f"新增 info 弹窗={n_new}")
    check("没有产生第二个并发循环", len(loop_threads()) == 1, f"loops={len(loop_threads())}")
    check("循环确实在提交", subs > app._state["submits"], f"{app._state['submits']} -> {subs}")
    app.stop_watch()
    schedule(0.6, lambda: phase4(subs))


def phase4(subs_before):
    check("停止循环后线程归零", len(loop_threads()) == 0, f"loops={len(loop_threads())}")
    check("停止后不再提交", app.client.submits == subs_before,
          f"{subs_before} -> {app.client.submits}")

    n = len(dialogs)
    app.grab_interval_var.set("0")
    app.start_watch()
    check("间隔为 0 被拒绝", len(dialogs) > n and dialogs[-1][0] == "error", str(dialogs[-1:]))
    check("间隔为 0 时没有起线程", not loop_threads())

    n = len(dialogs)
    app.grab_interval_var.set("1")
    app.grab_at_var.set("不是时间")
    app.start_watch()
    schedule(0.4, phase5)


def phase5():
    check("定时格式错误时线程自行退出", not loop_threads(), f"loops={len(loop_threads())}")
    app.grab_at_var.set("")
    schedule(0.05, phase6)


# ---------- 7) 收藏夹抢课成功路径 ----------
def phase6():
    saved = []
    favs = [{"teachingClassId": f"T{i}", "menuCode": "GG01", "courseName": f"课{i}"} for i in range(3)]
    gui.load_favorites = lambda: list(favs)
    gui.save_favorites = lambda f: saved.append(list(f))
    app.client = FakeClient(submit_code="1", poll_code="1")   # 全部成功
    app.fav_interval_var.set("0.05")
    app.fav_retry_var.set("0")
    app.fav_threads_var.set("2")
    app.fav_at_var.set("")
    app._refreshed = 0
    real_refresh = app.refresh_favorites

    def counting_refresh():
        app._refreshed += 1
        real_refresh()

    app.refresh_favorites = counting_refresh
    app.start_favgrab()
    app._saved = saved
    schedule(1.2, phase7)


def phase7():
    check("收藏夹抢课线程已回收",
          app.favgrab_thread is None or not app.favgrab_thread.is_alive())
    check("抢成功后写回收藏夹", app._saved and app._saved[-1] == [], f"写回={app._saved[-1:]}")
    check("抢成功后刷新了收藏夹界面", app._refreshed >= 1, f"refresh 次数={app._refreshed}")

    # ---------- 6) 后台线程写日志 ----------
    for i in range(5):
        threading.Thread(target=app._log, args=(f"后台日志{i}",), daemon=True).start()
    schedule(0.5, phase8)


def phase8():
    text = app.log_text.get("1.0", "end")
    missing = [i for i in range(5) if f"后台日志{i}" not in text]
    check("后台线程写日志不丢且不报错", not missing, f"缺失={missing}")

    # ---------- 5) 验证码弹窗异常路径 ----------
    before = len(root.winfo_children())
    for bad in (None, "data:image/gif;base64,bm90LWFuLWltYWdl"):
        try:
            gui.CaptchaDialog(root, bad)
            raised = False
        except Exception:
            raised = True
        check(f"验证码弹窗对坏数据抛错({str(bad)[:12]})", raised)
        check(f"坏数据不留残留窗口({str(bad)[:12]})", len(root.winfo_children()) == before,
              f"{before} -> {len(root.winfo_children())}")
        check(f"坏数据不抓输入焦点({str(bad)[:12]})",
              root.grab_current() is None or str(root.grab_current()) == "",
              f"grab={root.grab_current()}")

    # ---------- 8) 日志裁剪 ----------
    for i in range(2100):
        app._append_log(f"填充{i}")
    lines = int(app.log_text.index("end-1c").split(".")[0])
    check("日志行数被裁剪到上限内", lines <= 2000, f"lines={lines}")

    root.quit()


schedule(0.05, phase1)
root.after(15000, root.quit)      # 兜底，避免卡死
root.mainloop()
root.destroy()

# ---------- 8) 重复收藏项 ----------
dup = [{"teachingClassId": "T1"}, {"teachingClassId": "T1"}]
remain = gui._remaining_favorites(dup, [dup[0]])
check("重复收藏项只删成功的那条", remain == [dup[1]], f"剩余={len(remain)}")

# ---------- 10) 课程行字段映射 ----------
rows = list(gui.App._course_rows(
    [{"courseNumber": "N1", "courseName": "课", "tcList": [{"teachingClassID": "T1", "teacherName": "师"}]}],
    "GG01", "1"))
check("tcList 分支映射正确", len(rows) == 1 and rows[0][0] == "T1" and rows[0][6] == "-", str(rows))
rows = list(gui.App._course_rows([{"teachingClassID": "T2", "courseName": "课2", "campus": "仙林"}], "GG01", "1"))
check("扁平分支映射正确", len(rows) == 1 and rows[0][0] == "T2" and rows[0][4] == "仙林", str(rows))

# ---------- 9) 原子写 ----------
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "favorites.json")
    grab.save_favorites([{"teachingClassId": "T9"}], path=path)
    with open(path, encoding="utf-8") as f:
        check("原子写内容正确", json.load(f) == [{"teachingClassId": "T9"}])
    check("原子写不留临时文件", not os.path.exists(path + ".tmp"))

    real_replace = os.replace
    os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("模拟中断"))
    try:
        grab.save_favorites([{"teachingClassId": "T10"}], path=path)
    except OSError:
        pass
    finally:
        os.replace = real_replace
    with open(path, encoding="utf-8") as f:
        check("替换失败时旧文件完好", json.load(f) == [{"teachingClassId": "T9"}])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} 项未通过 -> {FAILS}")
    sys.exit(1)
print("ALL PASS")
