"""南京大学选课系统 GUI（保留原命令行版 grab.py）"""

import base64
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from grab import load_favorites, load_session, save_favorites, save_session
from nju_xk import NJUXKClient, XkError

try:
    import sv_ttk
except Exception:
    sv_ttk = None


def _remaining_favorites(favs, done):
    """抢课成功后从收藏夹剔除的项。

    按对象身份剔除（而不是值比较）：收藏夹里允许存在同 ID 的重复项，
    值比较会把没成功的那条一起删掉。favs 必须是传进抢课循环的同一个列表对象。
    """
    done_ids = {id(f) for f in done}
    return [f for f in favs if id(f) not in done_ids]


class CaptchaDialog(tk.Toplevel):
    def __init__(self, parent, image_data_url):
        super().__init__(parent)
        self.title("验证码：按顺序点击 4 个点")
        self.resizable(False, False)
        self.result = None
        self.pts = []
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        try:
            # 先校验并解析图片：任何一步失败都不能留下一个抓着输入焦点的空白窗口
            if not image_data_url or "," not in image_data_url:
                raise XkError("验证码图片数据为空")
            b64 = image_data_url.split(",", 1)[1]
            gif = base64.b64decode(b64)
            self.photo = tk.PhotoImage(data=gif)

            ttk.Label(self, text="请按顺序点击图中 4 个点").pack(padx=8, pady=(8, 4))
            self.canvas = tk.Canvas(self, width=self.photo.width(), height=self.photo.height(), highlightthickness=0)
            self.canvas.pack(padx=8, pady=4)
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
            self.canvas.bind("<Button-1>", self._click)

            self.tip = ttk.Label(self, text="已点击 0/4")
            self.tip.pack(pady=4)

            btns = ttk.Frame(self)
            btns.pack(fill="x", padx=8, pady=(0, 8))
            ttk.Button(btns, text="重置", command=self._reset).pack(side="left")
            ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")

            # grab 必须在窗口可见之后调用，否则部分平台会直接 grab 失败
            self.wait_visibility()
            self.grab_set()
        except Exception:
            try:
                self.grab_release()
            except tk.TclError:
                pass
            self.destroy()
            raise

    def _click(self, e):
        if len(self.pts) >= 4:
            return
        self.pts.append((int(e.x), int(e.y)))
        self.canvas.create_oval(e.x - 4, e.y - 4, e.x + 4, e.y + 4, fill="red", outline="white")
        self.tip.config(text=f"已点击 {len(self.pts)}/4")
        if len(self.pts) == 4:
            self.result = self.pts[:]
            self.destroy()

    def _reset(self):
        self.pts.clear()
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.tip.config(text="已点击 0/4")

    def _cancel(self):
        self.result = None
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("NJU 选课助手 GUI")
        self.root.geometry("1200x760")
        self.client = NJUXKClient()
        self.watch_stop = threading.Event()
        self.favgrab_stop = threading.Event()
        self.watch_thread = None
        self.favgrab_thread = None
        self._ui_queue = queue.Queue()

        self._build_top()
        self._build_tabs()
        self._load_saved_session()
        self._pump_ui()

    # ---------- 线程安全 ----------
    # Tk 只能在主线程操作：后台线程一律通过 _post_ui 投递，由主线程执行。

    def _post_ui(self, fn):
        """把一个函数排到主线程执行（可从任意线程调用）"""
        self._ui_queue.put(fn)

    def _pump_ui(self):
        """主线程定时消费后台线程投递的日志与界面更新"""
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:  # 单条更新失败不能打断整个泵
                print(f"[ui] 更新失败: {e}")
        self.root.after(100, self._pump_ui)

    def _run_async(self, name, work, on_done=None, alert=False):
        """后台线程跑网络操作，结果回到主线程，避免界面卡死"""
        def runner():
            try:
                result = work()
            except Exception as e:
                self._log(f"[错误] {name}: {e}")
                if alert:
                    self._post_ui(lambda: messagebox.showerror(name, str(e)))
                return
            if on_done is not None:
                self._post_ui(lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()

    def _build_top(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=8)

        self.status_var = tk.StringVar(value="未登录")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")

        self.theme_var = tk.StringVar(value="dark")
        ttk.Label(bar, text="  主题:").pack(side="left")
        cb = ttk.Combobox(bar, width=8, state="readonly", values=["dark", "light"], textvariable=self.theme_var)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_theme())

    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.tab_login = ttk.Frame(nb)
        self.tab_course = ttk.Frame(nb)
        self.tab_grab = ttk.Frame(nb)
        self.tab_fav = ttk.Frame(nb)
        nb.add(self.tab_login, text="登录/会话")
        nb.add(self.tab_course, text="菜单/课程")
        nb.add(self.tab_grab, text="抢课")
        nb.add(self.tab_fav, text="收藏夹")

        self._build_login_tab()
        self._build_course_tab()
        self._build_grab_tab()
        self._build_fav_tab()

    def _build_login_tab(self):
        f = ttk.Frame(self.tab_login)
        f.pack(fill="x", padx=12, pady=12)

        self.login_name = tk.StringVar()
        self.login_pwd = tk.StringVar()
        self.token_var = tk.StringVar()
        self.weu_var = tk.StringVar()
        self.student_var = tk.StringVar()
        self.batch_var = tk.StringVar()

        rows = [
            ("学号(登录)", self.login_name, False),
            ("密码", self.login_pwd, True),
            ("token", self.token_var, False),
            ("_WEU(可选)", self.weu_var, False),
            ("学号(token模式)", self.student_var, False),
            ("轮次代码(可选)", self.batch_var, False),
        ]
        for i, (label, var, pwd) in enumerate(rows):
            ttk.Label(f, text=label, width=15).grid(row=i, column=0, sticky="w", pady=4)
            ent = ttk.Entry(f, textvariable=var, show="*" if pwd else "")
            ent.grid(row=i, column=1, sticky="ew", pady=4)
        f.columnconfigure(1, weight=1)

        btns = ttk.Frame(f)
        btns.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(btns, text="账号密码登录", command=self.login_with_password).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="使用 token 加载", command=self.login_with_token).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="刷新菜单", command=self.refresh_menus).pack(side="left")

        self.menu_text = tk.Text(self.tab_login, height=20)
        self.menu_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_course_tab(self):
        top = ttk.Frame(self.tab_course)
        top.pack(fill="x", padx=12, pady=12)

        self.menu_code_var = tk.StringVar()
        self.keyword_var = tk.StringVar()

        ttk.Label(top, text="分类代码").pack(side="left")
        self.menu_cb = ttk.Combobox(top, width=14, textvariable=self.menu_code_var)
        self.menu_cb.pack(side="left", padx=6)
        ttk.Label(top, text="关键词").pack(side="left")
        ttk.Entry(top, textvariable=self.keyword_var, width=24).pack(side="left", padx=6)
        ttk.Button(top, text="查询课程", command=self.query_courses).pack(side="left", padx=6)

        cols = ("id", "course_no", "name", "teacher", "campus", "selected", "capacity", "menu", "kind")
        self.course_tree = ttk.Treeview(self.tab_course, columns=cols, show="headings", height=22)
        headings = {
            "id": "教学班ID",
            "course_no": "课程号",
            "name": "课程名",
            "teacher": "教师",
            "campus": "校区",
            "selected": "已选",
            "capacity": "容量",
            "menu": "分类",
            "kind": "courseKind",
        }
        for c in cols:
            self.course_tree.heading(c, text=headings[c])
            self.course_tree.column(c, width=120 if c != "name" else 180, anchor="w")
        self.course_tree.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_grab_tab(self):
        f = ttk.Frame(self.tab_grab)
        f.pack(fill="x", padx=12, pady=12)

        self.grab_menu_var = tk.StringVar()
        self.grab_tid_var = tk.StringVar()
        self.grab_kind_var = tk.StringVar()
        self.grab_interval_var = tk.StringVar(value="1.0")
        self.grab_retry_var = tk.StringVar(value="0")
        self.grab_at_var = tk.StringVar()

        rows = [
            ("分类代码", self.grab_menu_var),
            ("教学班ID", self.grab_tid_var),
            ("courseKind(可选)", self.grab_kind_var),
            ("间隔秒", self.grab_interval_var),
            ("最大次数(0不限)", self.grab_retry_var),
            ("定时(YYYY-MM-DD HH:MM:SS)", self.grab_at_var),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(f, text=label, width=22).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var).grid(row=i, column=1, sticky="ew", pady=4)
        f.columnconfigure(1, weight=1)

        btns = ttk.Frame(f)
        btns.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(btns, text="单次抢课", command=self.grab_once).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="开始循环抢课", command=self.start_watch).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="停止循环", command=self.stop_watch).pack(side="left")

    def _build_fav_tab(self):
        top = ttk.Frame(self.tab_fav)
        top.pack(fill="x", padx=12, pady=12)

        self.fav_tid_var = tk.StringVar()
        self.fav_menu_var = tk.StringVar()
        self.fav_name_var = tk.StringVar()
        self.fav_no_var = tk.StringVar()
        self.fav_kind_var = tk.StringVar()
        self.fav_interval_var = tk.StringVar(value="1.0")
        self.fav_retry_var = tk.StringVar(value="0")
        self.fav_at_var = tk.StringVar()
        self.fav_threads_var = tk.StringVar(value="3")

        items = [
            ("教学班ID", self.fav_tid_var), ("分类代码", self.fav_menu_var), ("课程名", self.fav_name_var),
            ("课程号", self.fav_no_var), ("courseKind", self.fav_kind_var),
            ("间隔秒", self.fav_interval_var), ("最大次数", self.fav_retry_var),
            ("定时", self.fav_at_var), ("线程数", self.fav_threads_var),
        ]
        for i, (label, var) in enumerate(items):
            r, c = divmod(i, 3)
            ttk.Label(top, text=label).grid(row=r * 2, column=c * 2, sticky="w", pady=(0, 2), padx=(0, 4))
            ttk.Entry(top, textvariable=var, width=18).grid(row=r * 2 + 1, column=c * 2, sticky="ew", pady=(0, 8), padx=(0, 8))
            top.columnconfigure(c * 2, weight=1)

        btns = ttk.Frame(top)
        btns.grid(row=8, column=0, columnspan=6, sticky="w")
        ttk.Button(btns, text="刷新收藏夹", command=self.refresh_favorites).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="添加收藏", command=self.add_favorite).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="移除所选", command=self.remove_selected_favorite).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="开始抢收藏夹", command=self.start_favgrab).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="停止抢收藏夹", command=self.stop_favgrab).pack(side="left")

        cols = ("id", "menu", "number", "name", "kind")
        self.fav_tree = ttk.Treeview(self.tab_fav, columns=cols, show="headings", height=12)
        for c, t in [("id", "教学班ID"), ("menu", "分类"), ("number", "课程号"), ("name", "课程名"), ("kind", "courseKind")]:
            self.fav_tree.heading(c, text=t)
            self.fav_tree.column(c, width=180 if c == "name" else 140, anchor="w")
        self.fav_tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        self.log_text = tk.Text(self.tab_fav, height=12)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.refresh_favorites()

    def _log(self, text):
        """线程安全：任意线程都能调用，真正的写入在主线程执行"""
        self._post_ui(lambda: self._append_log(text))

    def _append_log(self, text, max_lines=2000):
        self.log_text.insert("end", text + "\n")
        # 长时间循环抢课会不断写日志，裁剪掉太旧的行，避免无限膨胀
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self.log_text.delete("1.0", f"{lines - max_lines + 1}.0")
        self.log_text.see("end")

    def _load_saved_session(self):
        s = load_session()
        self.token_var.set(s.get("token", ""))
        self.student_var.set(s.get("studentCode", ""))
        self._apply_theme()

    def _apply_theme(self):
        if sv_ttk:
            try:
                sv_ttk.set_theme(self.theme_var.get())
            except Exception:
                pass

    def _require_login(self):
        if not self.client.token or not self.client.student_code:
            messagebox.showerror("错误", "请先登录或用 token 加载学籍信息")
            return False
        return True

    def _set_status(self):
        s = self.client.student.get("name") or ""
        c = self.client.student_code or ""
        b = self.client.batch.get("name") or ""
        self.status_var.set(f"已登录: {s}({c})  轮次: {b}")

    def login_with_password(self):
        name = self.login_name.get().strip()
        pwd = self.login_pwd.get()
        if not name or not pwd:
            messagebox.showerror("错误", "请输入学号和密码")
            return
        # 登录成功前不动 self.client，避免失败一次就把原有会话/token 清掉
        client = NJUXKClient(weu=self.weu_var.get().strip() or None)
        try:
            v = client.get_vcode()
            dlg = CaptchaDialog(self.root, v.get("image"))
            self.root.wait_window(dlg)
            if not dlg.result:
                raise XkError("已取消：未点满 4 个点")
            client.login(name, pwd, dlg.result, v.get("uuid"), v.get("vtoken"),
                         self.batch_var.get().strip() or None)
        except Exception as e:
            messagebox.showerror("登录失败", str(e))
            return
        self.client = client
        save_session(client.student_code, client.token)
        self.token_var.set(client.token or "")
        self.student_var.set(client.student_code or "")
        self.login_pwd.set("")          # 登录成功后不再把密码留在输入框里
        self._set_status()
        messagebox.showinfo("成功", "登录成功")
        # 刷新菜单是附带动作，它失败不能反过来报「登录失败」
        self.refresh_menus()

    def login_with_token(self):
        token = self.token_var.get().strip()
        student = self.student_var.get().strip()
        if not token:
            messagebox.showerror("错误", "请先填写 token")
            return
        if not student:
            saved = load_session().get("studentCode")
            if saved:
                self.student_var.set(saved)
                student = saved
        if not student:
            messagebox.showerror("错误", "请填写学号（或先用账号密码登录一次）")
            return
        client = NJUXKClient(token=token, weu=self.weu_var.get().strip() or None)
        try:
            client.load_student_info(student, self.batch_var.get().strip() or None)
        except Exception as e:
            messagebox.showerror("失败", str(e))
            return
        self.client = client
        save_session(client.student_code, client.token)
        self._set_status()
        messagebox.showinfo("成功", "token 登录成功")
        self.refresh_menus()

    def refresh_menus(self):
        if not self._require_login():
            return
        self.menu_text.delete("1.0", "end")
        menu_map = self.client.menu_map()
        codes = sorted(menu_map.keys())
        self.menu_cb["values"] = codes
        for c in codes:
            m = menu_map[c]
            self.menu_text.insert("end", f"{c}  {m.get('menuName', '')}  courseKind={m.get('courseKind', '')}\n")

    @staticmethod
    def _course_rows(courses, menu, kind):
        """把课程响应摊平成教学班行（字段映射只留一份）"""
        for c in courses:
            tc_list = c.get("tcList")
            if tc_list:
                for tc in tc_list:
                    yield (
                        tc.get("teachingClassID") or "",
                        c.get("courseNumber") or "",
                        c.get("courseName") or "",
                        tc.get("teacherName") or "",
                        tc.get("campusName") or "",
                        tc.get("numberOfSelected") or "-",
                        tc.get("classCapacity") or "-",
                        menu,
                        kind,
                    )
            else:
                yield (
                    c.get("teachingClassID") or "",
                    c.get("courseNumber") or "",
                    c.get("courseName") or "",
                    c.get("teacherName") or "",
                    c.get("campusName") or c.get("campus") or "",
                    c.get("numberOfSelected") or "-",
                    c.get("classCapacity") or "-",
                    menu,
                    kind,
                )

    def query_courses(self):
        if not self._require_login():
            return
        menu = self.menu_code_var.get().strip().upper()
        if not menu:
            messagebox.showerror("错误", "请输入分类代码")
            return
        kw = self.keyword_var.get().strip()
        # 菜单校验和 Tk 变量读取都放在主线程，后台线程不再碰 Tk
        try:
            kind = self.client.course_kind_of(menu)
        except XkError as e:
            messagebox.showerror("错误", str(e))
            return
        client = self.client.clone_for_thread()
        self._log(f"[课程] 查询 {menu} {kw or '(全部)'} ...")

        def done(courses):
            rows = list(self._course_rows(courses, menu, kind))
            self.course_tree.delete(*self.course_tree.get_children())
            for r in rows:
                self.course_tree.insert("", "end", values=r)
            self._log(f"[课程] {menu} 查询完成，共 {len(rows)} 条教学班记录")

        self._run_async("查询失败",
                        lambda: client.list_all_courses(menu, page_size=50, query_content=kw),
                        done, alert=True)

    def _wait_until(self, at_str, stop_event, client=None):
        if not at_str:
            return
        client = client or self.client
        target = datetime.strptime(at_str, "%Y-%m-%d %H:%M:%S")
        if target <= datetime.now():
            return
        self._log(f"[*] 等待到 {at_str} 开始...")
        last_ping = 0
        while datetime.now() < target and not stop_event.is_set():
            if time.time() - last_ping > 45:
                last_ping = time.time()
                try:
                    client.keep_alive()
                except Exception as e:
                    self._log(f"[!] 保活失败: {e}")
            time.sleep(0.1)

    def grab_once(self):
        if not self._require_login():
            return
        menu = self.grab_menu_var.get().strip().upper()
        tid = self.grab_tid_var.get().strip()
        kind = self.grab_kind_var.get().strip() or None
        if not menu or not tid:
            messagebox.showerror("错误", "请填写分类代码和教学班ID")
            return
        client = self.client.clone_for_thread()
        self._log(f"[提交] 单次抢课 {tid}@{menu} ...")

        def done(res):
            submit, polled = res
            self._log(f"[提交] code={submit.get('code')} msg={submit.get('msg')}")
            if polled:
                self._log(f"[处理] code={polled.get('code')} msg={polled.get('msg')}")
            else:
                self._log("[处理] 轮询超时")

        self._run_async("抢课", lambda: client.grab(tid, menu, kind), done)

    def start_watch(self):
        if not self._require_login():
            return
        if self.watch_thread is not None and self.watch_thread.is_alive():
            messagebox.showinfo("提示", "循环抢课已在运行中，请先点「停止循环」")
            return
        menu = self.grab_menu_var.get().strip().upper()
        tid = self.grab_tid_var.get().strip()
        kind = self.grab_kind_var.get().strip() or None
        at_str = self.grab_at_var.get().strip()
        if not menu or not tid:
            messagebox.showerror("错误", "请填写分类代码和教学班ID")
            return
        try:
            interval = float(self.grab_interval_var.get().strip() or "1")
            retry = int(self.grab_retry_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror("错误", "间隔/次数格式不正确")
            return
        if interval <= 0:
            messagebox.showerror("错误", "间隔秒必须大于 0")
            return

        self.watch_stop.clear()
        client = self.client.clone_for_thread()

        def run():
            count = 0
            start = time.time()
            try:
                self._wait_until(at_str, self.watch_stop, client)
            except ValueError:
                self._log("[错误] 定时格式应为 YYYY-MM-DD HH:MM:SS")
                return
            self._log(f"[*] 开始循环抢课: {tid}@{menu}")
            while not self.watch_stop.is_set():
                count += 1
                try:
                    submit = client.select_course(tid, menu, kind)
                    sc = str(submit.get("code"))
                    if sc == "1":
                        polled = client.poll(tid)
                        if polled and str(polled.get("code")) == "1":
                            self._log(f"[√] 第 {count} 次成功: {polled.get('msg') or ''}")
                            self.watch_stop.set()
                            return
                        self._log(f"[×] 第 {count} 次未通过: {(polled or {}).get('msg') or ''}")
                    elif sc == "302":
                        self._log("[!] token 已失效(302)")
                        self.watch_stop.set()
                        return
                    else:
                        self._log(f"[-] 第 {count} 次 code={sc} {submit.get('msg') or ''}")
                except Exception as e:
                    self._log(f"[-] 第 {count} 次异常: {e}")
                if retry and count >= retry:
                    self._log(f"[!] 已达最大次数 {retry}")
                    self.watch_stop.set()
                    return
                time.sleep(interval)
            self._log(f"[!] 已停止循环抢课（提交 {count} 次，用时 {time.time() - start:.1f}s）")

        def runner():
            try:
                run()
            finally:
                self.watch_thread = None

        self.watch_thread = threading.Thread(target=runner, daemon=True)
        self.watch_thread.start()

    def stop_watch(self):
        self.watch_stop.set()

    def refresh_favorites(self):
        favs = load_favorites()
        self.fav_tree.delete(*self.fav_tree.get_children())
        for f in favs:
            self.fav_tree.insert("", "end", values=(
                f.get("teachingClassId") or "",
                f.get("menuCode") or "",
                f.get("courseNumber") or "",
                f.get("courseName") or "",
                f.get("courseKind") or "",
            ))

    def add_favorite(self):
        tid = self.fav_tid_var.get().strip()
        menu = self.fav_menu_var.get().strip().upper()
        if not tid or not menu:
            messagebox.showerror("错误", "请填写教学班ID和分类代码")
            return
        favs = load_favorites()
        if any(f.get("teachingClassId") == tid and f.get("menuCode") == menu for f in favs):
            messagebox.showinfo("提示", "该课程已在收藏夹中")
            return
        kind = self.fav_kind_var.get().strip() or None
        if not kind and self.client.menus:
            try:
                kind = self.client.course_kind_of(menu)
            except Exception:
                kind = None
        favs.append({
            "teachingClassId": tid,
            "menuCode": menu,
            "courseKind": kind,
            "courseNumber": self.fav_no_var.get().strip(),
            "courseName": self.fav_name_var.get().strip(),
        })
        save_favorites(favs)
        self.refresh_favorites()

    def remove_selected_favorite(self):
        sel = self.fav_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要移除的收藏项")
            return
        # 同一个教学班ID可能在不同分类下都收藏过，按 (教学班ID, 分类) 精确删除
        keys = {(self.fav_tree.item(i, "values")[0], self.fav_tree.item(i, "values")[1]) for i in sel}
        favs = [f for f in load_favorites()
                if (f.get("teachingClassId"), f.get("menuCode")) not in keys]
        save_favorites(favs)
        self.refresh_favorites()

    def start_favgrab(self):
        if not self._require_login():
            return
        if self.favgrab_thread is not None and self.favgrab_thread.is_alive():
            messagebox.showinfo("提示", "收藏夹抢课已在运行中，请先点「停止抢收藏夹」")
            return
        favs = load_favorites()
        if not favs:
            messagebox.showerror("错误", "收藏夹为空")
            return
        at_str = self.fav_at_var.get().strip()
        try:
            interval = float(self.fav_interval_var.get().strip() or "1")
            retry = int(self.fav_retry_var.get().strip() or "0")
            threads = max(1, int(self.fav_threads_var.get().strip() or "1"))
        except ValueError:
            messagebox.showerror("错误", "间隔/次数/线程数格式不正确")
            return
        if interval <= 0:
            messagebox.showerror("错误", "间隔秒必须大于 0")
            return

        self.favgrab_stop.clear()
        client = self.client.clone_for_thread()

        def attempt(c, fav):
            label = f"{fav.get('courseName') or ''}({fav.get('courseNumber') or fav.get('teachingClassId')})"
            try:
                submit = c.select_course(fav["teachingClassId"], fav["menuCode"], fav.get("courseKind"))
                sc = str(submit.get("code"))
                if sc == "1":
                    polled = c.poll(fav["teachingClassId"])
                    if polled and str(polled.get("code")) == "1":
                        self._log(f"[√] {label} 抢课成功")
                        return "done"
                    self._log(f"[×] {label} 未通过，继续重试")
                    return "retry"
                if sc == "302":
                    self._log("[!] token 已失效(302)")
                    return "stop"
                self._log(f"[-] {label} code={sc} {submit.get('msg') or ''}")
                return "retry"
            except Exception as e:
                self._log(f"[-] {label} 异常: {e}")
                return "retry"

        def run_parallel():
            pending = list(favs)
            done = []
            cond = threading.Condition()
            inflight = [0]
            counter = [0]

            def worker():
                c = self.client.clone_for_thread()
                while not self.favgrab_stop.is_set():
                    with cond:
                        if retry and counter[0] >= retry:
                            self.favgrab_stop.set()
                            cond.notify_all()
                            return
                        while not pending and not self.favgrab_stop.is_set():
                            if inflight[0] == 0:
                                return
                            cond.wait(timeout=0.5)
                        if self.favgrab_stop.is_set():
                            return
                        fav = pending.pop(0)
                        inflight[0] += 1
                        counter[0] += 1
                    res = attempt(c, fav)
                    with cond:
                        inflight[0] -= 1
                        if res == "done":
                            done.append(fav)
                        elif res == "retry":
                            pending.append(fav)
                        else:
                            self.favgrab_stop.set()
                        cond.notify_all()
                    time.sleep(interval)

            try:
                self._wait_until(at_str, self.favgrab_stop, client)
            except ValueError:
                self._log("[错误] 收藏夹定时格式应为 YYYY-MM-DD HH:MM:SS")
                return

            self._log(f"[*] 开始抢收藏夹：{len(favs)} 门，线程={threads}")
            workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
            for t in workers:
                t.start()
            while any(t.is_alive() for t in workers):
                if self.favgrab_stop.is_set():
                    with cond:
                        cond.notify_all()
                time.sleep(0.2)

            if done:
                save_favorites(_remaining_favorites(favs, done))
                self._post_ui(self.refresh_favorites)   # UI 更新交回主线程
            self._log(f"[*] 收藏夹抢课结束：成功 {len(done)}/{len(favs)}")

        def runner():
            try:
                run_parallel()
            finally:
                self.favgrab_thread = None

        self.favgrab_thread = threading.Thread(target=runner, daemon=True)
        self.favgrab_thread.start()

    def stop_favgrab(self):
        self.favgrab_stop.set()
        self._log("[!] 已请求停止收藏夹抢课")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
