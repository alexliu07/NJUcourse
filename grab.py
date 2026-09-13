# -*- coding: utf-8 -*-
"""
南京大学选课系统 抢课程序

依赖: pip install requests pycryptodome

用法:
  python grab.py                            # 交互式完整流程（登录 -> 菜单 -> 列表 -> 选课）
  python grab.py --token <TOKEN>            # 已有 token，跳过登录
  python grab.py --token <TOKEN> menus      # 查看课程分类菜单
  python grab.py --token <TOKEN> list --menu GG01            # 列出某分类课程
  python grab.py --token <TOKEN> list --menu KZY --keyword 微积分   # 搜索
  python grab.py --token <TOKEN> select --id <教学班ID> --menu GG01     # 立即抢课(单次)
  python grab.py --token <TOKEN> watch --id <教学班ID> --menu GG01 \    # 自动循环抢课
         --interval 0.5
  python grab.py --token <TOKEN> watch --id <教学班ID> --menu GG01 \
         --at "2026-09-14 13:30:00" --interval 0.3 --retry 500   # 到点开抢，0.3s 间隔，最多 500 次

  # 本地收藏夹（不依赖网站自带收藏）
  python grab.py --token <TOKEN> favadd --id <教学班ID> --menu GG01 --name 演示物理
  python grab.py --token <TOKEN> favlist
  python grab.py --token <TOKEN> favremove --id <教学班ID>
  python grab.py --token <TOKEN> favgrab --at "2026-09-14 13:30:00" --interval 0.3  # 自动抢收藏夹全部课程
"""
import argparse
import base64
import getpass
import json
import os
import sys
import tempfile
import time
from datetime import datetime

from nju_xk import NJUXKClient, XkError

FAV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favorites.json")


# ---------------- 本地收藏夹 ----------------


def load_favorites(path=FAV_FILE):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_favorites(favs, path=FAV_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)


def print_favorites(favs):
    if not favs:
        print("（收藏夹为空，用 favadd 添加）")
        return
    print(f"{'序号':>4} {'课程号':<12} {'课程名':<20} {'分类':<8}  教学班ID")
    for i, f in enumerate(favs, 1):
        print(f"{i:>4} {(f.get('courseNumber') or ''):<12} {(f.get('courseName') or ''):<20} "
              f"{(f.get('menuCode') or ''):<8}  {f.get('teachingClassId')}")


def loop_grab_favorites(client, favs, interval=1.0, max_retries=0):
    """自动抢收藏夹里的所有课程：循环遍历，成功一门移除一门。"""
    if not favs:
        print("收藏夹为空，无法抢课")
        return False
    pending = list(favs)
    done = []
    count = 0
    start = time.time()
    print(f"[*] 开始自动抢收藏夹 {len(pending)} 门课：间隔={interval}s "
          f"({'不限次数' if not max_retries else '最多' + str(max_retries) + '次'})  Ctrl+C 停止")
    try:
        while pending:
            for fav in pending[:]:
                count += 1
                label = f"{fav.get('courseName') or ''}({fav.get('courseNumber') or fav.get('teachingClassId')})"
                try:
                    submit = client.select_course(fav["teachingClassId"], fav["menuCode"], fav.get("courseKind"))
                    sc = str(submit.get("code"))
                    if sc == "1":
                        polled = client.poll(fav["teachingClassId"])
                        if polled and str(polled.get("code")) == "1":
                            print(f"[√] {label} 抢课成功！{polled.get('msg') or ''}")
                            done.append(fav)
                            pending.remove(fav)
                        else:
                            msg = (polled or {}).get("msg")
                            print(f"[×] {label} 已受理但未通过（{msg or (polled and polled.get('code'))}），继续重试...")
                    elif sc == "302":
                        print("[!] 登录已失效(302)，请重新登录获取 token 后重试")
                        return False
                    else:
                        print(f"[-] {label}: code={sc} {submit.get('msg') or ''}")
                except XkError as e:
                    print(f"[-] {label} 请求异常: {e}")
                if max_retries and count >= max_retries:
                    print(f"[!] 已达最大提交次数 {max_retries}，停止")
                    return False
                time.sleep(interval)
        print(f"[√] 收藏夹全部抢完！成功 {len(done)} 门，用时 {time.time() - start:.1f}s")
        return True
    except KeyboardInterrupt:
        print(f"\n[!] 已手动停止（成功 {len(done)} 门，剩 {len(pending)} 门，用时 {time.time() - start:.1f}s）")
        return False


# ---------------- 验证码（人机验证） ----------------

def solve_captcha(image_data_url):
    """弹出窗口让用户按顺序点击 4 个点，返回 [(x,y), ...]。
    无法弹窗时回退为：保存图片 + 手动输入坐标。"""
    if not image_data_url or "," not in image_data_url:
        raise XkError("验证码图片数据为空")
    b64 = image_data_url.split(",", 1)[1]
    gif = base64.b64decode(b64)
    path = os.path.join(tempfile.gettempdir(), "nju_vcode.gif")
    with open(path, "wb") as f:
        f.write(gif)

    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("验证码：请按顺序点击 4 个点")
        img = tk.PhotoImage(file=path)
        cv = tk.Canvas(root, width=img.width(), height=img.height(), highlightthickness=0)
        cv.pack()
        cv.create_image(0, 0, anchor="nw", image=img)
        pts = []

        def click(e):
            pts.append((int(e.x), int(e.y)))
            cv.create_oval(e.x - 4, e.y - 4, e.x + 4, e.y + 4, fill="red", outline="white")
            if len(pts) >= 4:
                root.quit()

        cv.bind("<Button-1>", click)
        root.mainloop()
        root.destroy()
        if len(pts) < 4:
            raise XkError("已取消：未点满 4 个点")
        return pts
    except XkError:
        raise
    except Exception as e:
        print(f"[!] 无法弹出图形窗口（{e}）")
        print(f"    已保存验证码图片到: {path}")
        print("    请用看图软件打开，按顺序记录 4 个点击点的坐标（相对图片左上角，格式 x,y，如 97,27）")
        pts = []
        for i in range(4):
            while True:
                s = input(f"    第 {i + 1} 个点 x,y: ").strip()
                try:
                    x, y = s.replace("，", ",").split(",")
                    pts.append((int(x), int(y)))
                    break
                except ValueError:
                    print("    格式错误，请输入 x,y")
        return pts


# ---------------- 输出辅助 ----------------

def print_menu_tree(client):
    menus = client.menus
    if not menus:
        print("（无菜单数据）")
        return
    tops = [m for m in menus if not m.get("parentMenuCode")]
    for m in tops:
        limit = m.get("limitNumber") or "0"
        credit = m.get("limitCredit") or "0"
        extra = []
        if limit and limit != "0":
            extra.append(f"限{limit}门")
        if credit and credit != "0":
            extra.append(f"限{credit}学分")
        print(f"  {m.get('menuCode'):6s} {m.get('menuName')}  {' '.join(extra)}")
        subs = [x for x in menus if x.get("parentMenuCode") == m.get("menuCode")]
        for x in subs:
            print(f"         └─ {x.get('menuCode'):6s} {x.get('menuName')}")


DAY_NAMES = {"1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六", "7": "周日"}


def fmt_time_place(c):
    """从 teachingTimeList 组装“周X a-b节 周次 地点”字符串"""
    times = c.get("teachingTimeList") or []
    parts = []
    for t in times:
        d = DAY_NAMES.get(str(t.get("dayOfWeek")), "")
        bs, es = t.get("beginSection"), t.get("endSection")
        wk = t.get("weekName") or ""
        place = t.get("teachingPlace") or ""
        seg = []
        if d:
            seg.append(d)
        if bs is not None and es is not None and str(bs) != "0" and str(es) != "0":
            seg.append(f"{bs}-{es}节")
        if wk:
            seg.append(wk)
        if place:
            seg.append(place)
        if seg:
            parts.append(" ".join(seg))
    if parts:
        return " / ".join(parts)
    # 无教学时间时回退到课程级地点
    return c.get("teachingPlace") or ""


def print_courses(courses, limit=30):
    if not courses:
        print("（没有课程数据）")
        return
    print(f"{'序号':>4} {'课程号':<12} {'课程名':<16} {'学分':>4} {'教师':<10} "
          f"{'时间地点':<26} {'校区':<8} {'已选/容量':>9}  教学班ID")
    for i, c in enumerate(courses[:limit], 1):
        tid = c.get("teachingClassID") or ""
        num = c.get("courseNumber") or ""
        name = (c.get("courseName") or "")[:16]
        credit = c.get("credit") or ""
        teacher = (c.get("teacherName") or "")[:10]
        tp = fmt_time_place(c)[:26]
        campus = (c.get("campusName") or c.get("campus") or "")[:8]
        cnt = c.get("numberOfSelected") or "-"
        cap = c.get("classCapacity") or "-"
        chosen = "★" if str(c.get("isChoose")) == "1" else " "
        print(f"{i:>4} {num:<12} {chosen}{name:<15} {credit:>4} {teacher:<10} "
              f"{tp:<26} {campus:<8} {cnt:>4}/{cap:<4}  {tid}")
    if len(courses) > limit:
        print(f"  ... 共 {len(courses)} 门，只显示前 {limit} 门")


# ---------------- 流程 ----------------

def do_login(client, batch_code=None):
    vcode = client.get_vcode()
    name = input("学号: ").strip()
    pwd = getpass.getpass("统一身份认证密码: ")
    pts = solve_captcha(vcode["image"])
    print(f"验证码点: {pts}")
    info = client.login(name, pwd, pts, vcode["uuid"], vcode["vtoken"], batch_code=batch_code)
    print(f"\n登录成功：{info.get('name')} ({info.get('code')})")
    print(f"当前轮次：{client.batch.get('name')}  开放时间: {client.batch.get('beginTime')}")
    print(f"token: {client.token}\n")
    return client


def interactive(client):
    print("=" * 60)
    print("课程分类菜单：")
    print_menu_tree(client)
    print("=" * 60)
    while True:
        cmd = input("\n命令: menus(菜单) / list(列课程) / select(抢课) / fav(收藏夹) / favadd(加收藏) / quit(退出)\n> ").strip().lower()
        if cmd in ("q", "quit", "exit"):
            break
        if cmd in ("m", "menus"):
            print_menu_tree(client)
        elif cmd in ("l", "list"):
            menu = input("分类代码(如 KZY/GG01/ZY): ").strip().upper()
            kw = input("搜索关键词(直接回车=全部): ").strip()
            try:
                courses = client.search_courses(menu, kw, page_size=50)
                print_courses(courses)
            except XkError as e:
                print(f"[错误] {e}")
        elif cmd in ("s", "select"):
            menu = input("分类代码: ").strip().upper()
            tid = input("教学班ID(teachingClassID): ").strip()
            try:
                print(f"提交选课: {tid} @ {menu}")
                submit, polled = client.grab(tid, menu)
                print(f"提交结果: {submit.get('msg') or submit.get('code')}")
                if polled:
                    print(f"处理结果: code={polled.get('code')} msg={polled.get('msg')}")
                else:
                    print("轮询超时（可能仍在处理中）")
            except XkError as e:
                print(f"[错误] {e}")
        elif cmd == "fav":
            favs = load_favorites()
            print_favorites(favs)
            if favs:
                print("可用 favgrab 命令自动抢收藏夹全部课程")
        elif cmd == "favadd":
            menu = input("分类代码: ").strip().upper()
            tid = input("教学班ID(teachingClassID): ").strip()
            name = input("课程名(可选，直接回车跳过): ").strip()
            num = input("课程号(可选，直接回车跳过): ").strip()
            favs = load_favorites()
            if any(f.get("teachingClassId") == tid and f.get("menuCode") == menu for f in favs):
                print("该课程已在收藏夹中")
            else:
                kind = None
                if client.menus:
                    try:
                        kind = client.course_kind_of(menu)
                    except XkError:
                        kind = None
                favs.append({"teachingClassId": tid, "menuCode": menu, "courseKind": kind,
                             "courseNumber": num, "courseName": name})
                save_favorites(favs)
                print(f"已添加 {name or tid}，当前共 {len(favs)} 门")
        else:
            print("未知命令")


def wait_until(target_str, client=None):
    target = datetime.strptime(target_str, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    if target <= now:
        return
    print(f"[*] 等待到 {target_str} 开始抢课... (当前 {now:%Y-%m-%d %H:%M:%S})")
    last_ping = 0
    warned = False
    while datetime.now() < target:
        # 每 45 秒做一次轻量请求，尽量保持会话活跃
        if client and time.time() - last_ping > 45:
            last_ping = time.time()
            try:
                client.keep_alive()
            except XkError as e:
                if not warned:
                    print(f"[!] 保活请求失败（token 可能已过期）：{e}，请重新登录")
                    warned = True
        time.sleep(0.1)


def loop_grab(client, tid, menu, kind=None, interval=1.0, max_retries=0):
    """按固定时间间隔持续提交选课请求，直到成功 / 达到次数上限 / Ctrl+C 停止。"""
    count = 0
    start = time.time()
    print(f"[*] 开始自动抢课: 教学班={tid} 分类={menu} 间隔={interval}s "
          f"({'不限次数' if not max_retries else '最多' + str(max_retries) + '次'})  Ctrl+C 停止")
    try:
        while True:
            count += 1
            try:
                submit = client.select_course(tid, menu, kind)
                sc = str(submit.get("code"))
                if sc == "1":
                    polled = client.poll(tid)
                    if polled and str(polled.get("code")) == "1":
                        print(f"[√] 第 {count} 次提交：抢课成功！{polled.get('msg') or ''}")
                        return True
                    msg = (polled or {}).get("msg")
                    print(f"[×] 第 {count} 次提交：已受理但未通过（{msg or polled and polled.get('code')}），继续重试...")
                elif sc == "302":
                    print("[!] 登录已失效(302)，请重新登录获取 token 后重试")
                    return False
                else:
                    print(f"[-] 第 {count} 次：code={sc} {submit.get('msg') or ''}")
            except XkError as e:
                print(f"[-] 第 {count} 次：请求异常 {e}")
            if max_retries and count >= max_retries:
                print(f"[!] 已达最大提交次数 {max_retries}，停止")
                return False
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[!] 已手动停止（共提交 {count} 次，用时 {time.time() - start:.1f}s）")
        return False


def main():
    ap = argparse.ArgumentParser(description="南京大学选课系统抢课程序")
    ap.add_argument("--token", help="已有的登录 token（跳过登录）")
    ap.add_argument("--weu", help="WAF 校验 cookie _WEU（可选，一般自动获取）")
    ap.add_argument("--student", help="学号（--token 免登录时需提供）")
    ap.add_argument("--batch", help="选课轮次代码（可选，默认自动选可选的轮次）")
    ap.add_argument("command", nargs="?", default=None,
                    choices=["menus", "list", "select", "watch",
                             "favadd", "favlist", "favremove", "favgrab"],
                    help="menus/list/select/watch/favadd/favlist/favremove/favgrab")
    ap.add_argument("--menu", help="分类代码，如 ZY/GG/KZY/TX/TY/GG01/GG02/GG06/MY/TX01...")
    ap.add_argument("--id", dest="tid", help="教学班ID teachingClassID")
    ap.add_argument("--name", help="课程名（收藏夹展示用，可选）")
    ap.add_argument("--number", help="课程号（收藏夹展示用，可选）")
    ap.add_argument("--keyword", default="", help="搜索关键词")
    ap.add_argument("--kind", dest="kind", help="courseKind（一般无需指定）")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("--at", dest="at", help="定时抢课时间，格式 YYYY-MM-DD HH:MM:SS")
    ap.add_argument("--interval", type=float, default=1.0, help="自动抢课提交间隔(秒)，默认 1.0")
    ap.add_argument("--retry", type=int, default=0, help="最大提交次数，0=不限（默认）")
    ap.add_argument("--loop", action="store_true", help="select 时循环提交，等价于 watch")
    args = ap.parse_args()

    client = NJUXKClient(token=args.token, weu=args.weu)

    local_commands = ("favadd", "favlist", "favremove")

    # 抢收藏夹前先检查是否为空（无需登录）
    if args.command == "favgrab" and not load_favorites():
        print("收藏夹为空，请先用 favadd 添加")
        sys.exit(1)

    # 本地收藏夹命令无需登录
    if args.command not in local_commands:
        # 登录
        if not client.token:
            try:
                do_login(client, args.batch)
            except XkError as e:
                print(f"[登录失败] {e}")
                sys.exit(1)
            print("提示：可用 --token 参数复用上面的 token，下次免登录")
        else:
            # token 模式：需要学籍信息与轮次的命令
            need_student = args.command in ("menus", "list", "select", "watch", "favgrab") or args.command is None
            if need_student and not client.student:
                student_code = args.student or input("学号: ").strip()
                try:
                    client.load_student_info(student_code, args.batch)
                    print(f"当前轮次：{client.batch.get('name')}  开放时间: {client.batch.get('beginTime')}")
                except XkError as e:
                    print(f"[错误] {e}")
                    sys.exit(1)

    # 仅登录（交互模式）
    if args.command is None:
        interactive(client)
        return

    try:
        if args.command == "menus":
            print_menu_tree(client)
        elif args.command == "list":
            if not args.menu:
                print("请用 --menu 指定分类代码")
                sys.exit(1)
            courses = client.search_courses(args.menu.upper(), args.keyword, page_size=max(args.size, 10))
            print_courses(courses)
        elif args.command in ("select", "watch"):
            if not args.menu or not args.tid:
                print("请用 --menu 和 --id 指定分类与教学班ID")
                sys.exit(1)
            if args.at:
                wait_until(args.at, client)
            if args.command == "watch" or args.loop:
                # 自动循环抢课
                loop_grab(client, args.tid, args.menu.upper(), args.kind, args.interval, args.retry)
            else:
                print(f"[*] 抢课: teachingClassId={args.tid} menu={args.menu}")
                submit, polled = client.grab(args.tid, args.menu.upper(), args.kind)
                print(f"提交: code={submit.get('code')} msg={submit.get('msg')}")
                if polled:
                    print(f"处理: code={polled.get('code')} msg={polled.get('msg')}")
                    if str(polled.get("code")) == "1":
                        print("[√] 抢课成功！")
                    else:
                        print("[×] 抢课失败")
                else:
                    print("[?] 轮询超时，请稍后查看已选课程")
        elif args.command == "favlist":
            print_favorites(load_favorites())
        elif args.command == "favadd":
            if not args.tid or not args.menu:
                print("请用 --id 和 --menu 指定教学班ID与分类代码")
                sys.exit(1)
            favs = load_favorites()
            menu = args.menu.upper()
            for f in favs:
                if f.get("teachingClassId") == args.tid and f.get("menuCode") == menu:
                    print("该课程已在收藏夹中")
                    sys.exit(0)
            kind = args.kind
            if not kind and client.menus:
                try:
                    kind = client.course_kind_of(menu)
                except XkError:
                    kind = None
            favs.append({"teachingClassId": args.tid, "menuCode": menu, "courseKind": kind,
                         "courseNumber": args.number or "", "courseName": args.name or ""})
            save_favorites(favs)
            print(f"已添加: {args.name or args.tid} ({menu})，当前共 {len(favs)} 门")
        elif args.command == "favremove":
            if not args.tid:
                print("请用 --id 指定教学班ID")
                sys.exit(1)
            favs = load_favorites()
            new = [f for f in favs if f.get("teachingClassId") != args.tid]
            if len(new) == len(favs):
                print(f"收藏夹中不存在 {args.tid}")
            else:
                save_favorites(new)
                print(f"已移除 {args.tid}，当前共 {len(new)} 门")
        elif args.command == "favgrab":
            favs = load_favorites()
            if not favs:
                print("收藏夹为空，请先用 favadd 添加")
                sys.exit(1)
            if args.at:
                wait_until(args.at, client)
            loop_grab_favorites(client, favs, args.interval, args.retry)
    except XkError as e:
        print(f"[错误] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
