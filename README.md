# 南京大学选课系统抢课程序

基于对 `https://xk.nju.edu.cn/` 选课系统的逆向分析，用 Python 实现登录、课程列表查询与抢课（志愿式选课）。

## 技术栈

- **语言**：Python 3
- **HTTP 客户端**：[requests](https://pypi.org/project/requests/)
- **加密**：
  - [pycryptodome](https://pypi.org/project/pycryptodome/) —— 选课参数 AES-128-ECB 加密
  - 纯 Python 实现的 DES（三密钥）—— 登录密码加密，与网页 `des.min.js` 逐字节一致
- **验证码点选**：tkinter（标准库，无需额外安装）
- **标准库**：`argparse`、`getpass`、`json`、`base64`、`datetime` 等

## 功能

- 登录（含验证码：自动弹窗，在图上按顺序点击 4 个点）
- 课程分类菜单查询（专业/公共/跨专业/通修/体育等）
- 课程搜索与列表展示
- 抢课：提交 `volunteer.do` 并轮询处理结果
- 定时抢课（`--at` 到点才提交）
- 已有 token 免登录（`--token`）
- **本地收藏夹**：收藏课程 + 一键自动抢收藏夹全部课程（数据存在本地 `favorites.json`，不用网站自带收藏）

## 安装

```bash
pip install -r requirements.txt
```

## 用法

```bash
# 交互式完整流程（登录 -> 菜单 -> 列课程 -> 选课）
python grab.py

# 已有 token，跳过登录
python grab.py --token <TOKEN>

# 查看课程分类菜单
python grab.py --token <TOKEN> menus

# 列出某分类课程
python grab.py --token <TOKEN> list --menu GG01

# 搜索课程
python grab.py --token <TOKEN> list --menu KZY --keyword 微积分

# 列表会自动翻页拉全，终端内分页浏览（回车下一页，q 退出）；--size 调整每页条数
python grab.py --token <TOKEN> list --menu GG01 --size 30

# 立即抢课（单次，--id 为教学班ID teachingClassID）
python grab.py --token <TOKEN> select --id 2026202717800104001 --menu GG01

# 自动循环抢课（按固定间隔持续提交，直到成功或 Ctrl+C）
python grab.py --token <TOKEN> watch --id 2026202717800104001 --menu GG01 --interval 0.5

# 到点开抢：先等到指定时间，再以 0.3s 间隔最多提交 500 次
python grab.py --token <TOKEN> watch --id 2026202717800104001 --menu GG01 \
    --at "2026-09-14 13:30:00" --interval 0.3 --retry 500
```

### 本地收藏夹

```bash
# 添加课程到收藏夹（--id 为教学班ID，--name/--number 仅用于展示）
python grab.py favadd --id 2026202717800104001 --menu GG01 --name 演示物理 --number 78001040

# 查看收藏夹（无需登录）
python grab.py favlist

# 移除某门课
python grab.py favremove --id 2026202717800104001

# 自动抢收藏夹里所有课程（到点开抢，0.3s 间隔）
python grab.py --token <TOKEN> --student 261180197 favgrab \
    --at "2026-09-14 13:30:00" --interval 0.3
```

- 收藏数据存在本地 `favorites.json`，与网站自带收藏无关，不依赖登录。
- `favgrab` 会循环遍历收藏夹，成功一门移除一门，直到全部抢完或 `Ctrl+C` 停止。
- `favgrab` 支持 `--at` / `--interval` / `--retry` 参数，同 `watch`。

### 自动抢课参数

| 参数 | 说明 |
|---|---|
| `--interval` | 两次提交之间的间隔（秒），默认 `1.0` |
| `--retry` | 最大提交次数，`0` = 不限（默认） |
| `--at` | 先等待到指定时间再开始提交 |
| `--loop` | 让 `select` 也变成循环模式（等价于 `watch`） |

> 提示：每次提交都会用新时间戳重新加密参数（服务端防重放），所以循环重复提交是有效的。建议间隔不要太小，避免给服务器造成压力。

> **重要**：登录 token 有有效期限（约十几到几十分钟），过期后接口返回 `302 非法请求`。所以：
> - 用 `--token` 免登录时，请在开抢前重新取一个新鲜 token；
> - 用完整登录流程（不带 `--token`）时，建议开抢前几分钟再运行，`--at` 只适合短时间等待（程序等待期间会每 45 秒自动保活，但不能保证一定不过期）。

## 手动获取 token 和 _WEU（跳过登录）

如果不想让程序处理验证码，可以在浏览器登录后手动取两个值：

1. 浏览器登录进入选课页。
2. 按 `F12` 打开开发者工具 → `Console` 控制台。
3. 执行以下命令获取值：

```js
console.log('TOKEN=' + sessionStorage.token)
console.log('WEU=' + (document.cookie.match(/_WEU=([^;]*)/) || [])[1])
```

4. 把结果填到程序参数：

```bash
python grab.py --token <TOKEN> --weu <WEU>
```

> `_WEU` 是 WAF 校验 cookie，`elective/*` 接口必须携带。程序在登录时也会自动获取（`login.do` 响应会下发），一般无需手动传。

## 专业课（ZY）说明

专业课列表结构与其他类别不同：`list --menu ZY` 返回的是「课程 + 内嵌教学班列表」，会逐门展开显示每个教学班的**教师 / 校区 / 地点 / 已选容量 / 教学班ID**。选课时：

```bash
# 先列出专业课，记下目标教学班的 teachingClassID
python grab.py --token <TOKEN> list --menu ZY

# 用教学班ID抢课（提交接口与其他类别相同，courseKind 会自动取 ZY 菜单的 "1"）
python grab.py --token <TOKEN> select --id <教学班ID> --menu ZY
```

## 说明

- **登录密码加密**：DES（三密钥 `this/password/is`）→ 大写 hex → Base64，已与网页逐字节对齐。
- **选课参数加密**：明文 JSON 追加 `?timestrap=<毫秒时间戳>` 后 AES-128-ECB（密钥 `wHm1xj3afURghi0c`）→ Base64。
- **抢课核心**：`POST /sys/xsxkapp/elective/volunteer.do`（参数 `addParam` 为 AES 密文 + `studentCode`），提交后每秒轮询 `/sys/xsxkapp/elective/studentstatus.do` 直到 `code=1/-1`。
- 选课本质是**志愿式**：把课程加入志愿后由服务器异步处理，前端轮询结果。
- 未到开放时间会返回 `当前时间不在选课开放时间范围内`。

> 仅供个人选课使用，请遵守学校规定，不要高频请求影响服务器。
