# ec2-i4i-capacity-grabber

一个脚本，24×7 死等抢 **i4i** EC2 容量，给 Prime Day 等大促囤产能。
抢的是 **On-Demand 容量预留（ODCR）**：产能锁在你名下，实例停了也不丢，配合客户 ASG 自动吸纳。

```bash
# 演练（不花钱，先看计划）→ 实弹（⚠️ 立刻计费）
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
```

> 上面这条就是本次 Prime Day 的标准命令：两个 AZ 各抢 5000 vCPU，合计 **10000 vCPU**（≈156 台 i4i.16xlarge）。

---

## 30 秒了解

- **目标**：抢 10000 vCPU 的 i4i，均匀铺在 `us-east-1b` / `us-east-1d` 两个 AZ。
- **怎么抢**：每次建一个 `i4i.16xlarge`（64 核）的 open 预留，累加到目标；产能是间歇放出来的，所以 `--watch` 死等。
- **怎么用上**：客户 EKS 的 ASG 设 `capacity-reservations-first`，扩容时实例自动落进预留。**脚本不碰客户 ASG**。
- **三件事必须先做对**（否则白忙）：
  1. **抢预留的账号 = EKS 集群账号**（open 预留只在同账号内匹配）。
  2. **vCPU 配额提到 ≥ 12000**（默认才 5，是头号阻塞，要提前开工单）→ 见 [`扩大.md`](扩大.md)。
  3. **客户 ASG 配好** → 见 [`客户配置.md`](客户配置.md)。

---

## 三步上手

**1. 装**

```bash
pip install -r requirements.txt   # 只需 boto3，Python 3.8+
```

凭证用环境变量 / `~/.aws/credentials` / IAM role 都行。在 EC2 上长跑推荐 **instance profile**。

**2. 演练**（默认就是 dry-run，不加 `--live` 不花一分钱）

```bash
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000
```

看打印的计划：抢哪些 AZ、机型、每 AZ 上限、总目标对不对。

**演练这一趟到底做了什么**——它把「能不能跑通」全验一遍，唯独不真建、不花钱：

- ✅ **真连 AWS**：用你的凭证调只读 API（列 AZ、查 `i4i.16xlarge` 在哪些 AZ 有供货），所以**凭证错、IAM 权限不够、账号/区域不对会当场报错**。
- ✅ **真算计划**：`--per-az-cores 5000` × 2 AZ → 总目标 10000，打印出来给你核对。
- ✅ **真试建预留、但带 `DryRun` 标志**：对每个机型/AZ 调一次创建预留的 API，AWS 只校验「参数合法吗、你有没有权限建」然后拒绝真正创建，脚本据此打印 `[dry-run] would reserve ...`。**所以连"有没有权限建预留"都验到了**。
- ❌ **不建任何预留、不计费、不写台账**（`grabs.jsonl` 只记真实抢占，保持干净）。

> 一句话：演练 = 把权限、账号、AZ/机型供货、计划数字全验一遍，**就差没真按下扣费键**。看着没问题，再加 `--live`。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

```bash
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
```

挂在 systemd 里 24×7 跑（见下）。抢够了它自己停。

**用完一定要释放，停止计费：**

```bash
python3 grab_odcr.py --cancel-all --live
```

---

## 常用命令

```bash
python3 grab_odcr.py --list                          # 看当前抢了多少（含 per-AZ + 总计汇总，自动带目标进度）
watch -n 30 'python3 grab_odcr.py --list'            # 持续盯进度，每 30 秒刷新，Ctrl+C 停
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30   # 按总核数抢（不分 AZ）
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 --live --watch   # 按机型分别抢各自台数
python3 grab_odcr.py ... --live --end-hours 6        # 计费保险：6 小时后预留自动过期
python3 grab_odcr.py --cancel-all --live             # 释放全部、停止计费
```

### 关键参数

| 参数 | 作用 |
|------|------|
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能是间歇放出来的，必须死等） |
| `--per-az-cores N` | 每个 AZ 各封顶 N 核，均衡铺货（对齐 ASG 50/50）。**客户每 AZ 一个 ASG 时务必用**。设了它就**不用再写 `--target-cores`**：总目标自动 = `N × AZ数`（如 `5000 × 2 = 10000`），启动日志会打印 `balanced mode: per-az cap 5000 vCPU x 2 AZ -> target 10000 vCPU` 确认 |
| `--target-cores N` | 总共抢够 N 核就停。**字面默认 8**（很小，仅占位），但只要设了 `--per-az-cores` 且没手动改它，就会被自动覆盖成 `N × AZ数`。⚠️ 两个都写且对不上（如 per-az 5000 但 target 9000）时，**以 `--target-cores` 为硬总闸**——别写对不上的值，要么只写 `--per-az-cores`，要么保证 `target = per_az × AZ数` |
| `--per-type TYPE:COUNT ...` | **按机型分别抢**：每种机型一个**独立台数目标**，各抢各的，某种没货不拖累其他。如 `--per-type i4i.16xlarge:10 i4i.8xlarge:5`。覆盖 `--target-cores`/`--per-az-cores`。见下方「按机型分别抢」 |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d` |
| `--region R` | 区域，默认 `us-east-1` |
| `--cancel-all` | 取消全部预留、停止计费 |
| `--list` | 只看不动（列每条预留 + per-AZ/总计核数汇总，自动从台账读目标显示进度） |

完整参数 `python3 grab_odcr.py --help`。

> **重启幂等**：上限按「AWS 实时持有的核数」判断（不是进程内存），所以进程崩溃 / 机器重启后续跑，会按各 AZ 真实持有**只补差额**，不超抢、不抢歪。配 systemd `Restart=always` 安全。

---

## 24×7 在 EC2 上跑（systemd）

开一台 `t3.micro`（脚本几乎不耗资源），用 instance profile 拿凭证，丢给 systemd 长跑——断了自动拉起、机器重启自启。这样大促期间脚本**一直挂着死等**，你不用反复敲命令，只需偶尔看进度（见下方「监控」）。

> **三个阶段，三个不同时间点，别搞混**：
> **大促前**部署就位 → **大促中**挂着不动、只看监控 → **大促后**先 `stop` 再 `cancel-all`。
> ⚠️ **`stop` 和 `cancel-all` 是两码事**：`stop` 只停止「继续抢」，**已抢到的预留还在、还在计费**；只有 `cancel-all` 才真正释放预留、停止计费。这俩**绝不能串成一条命令**——大促期间你可能会 `stop`（比如改配置重启），但**绝不能 cancel**。

### 阶段一：大促前 —— 部署就位（提前做好）

**1. 新建 systemd 服务文件**（把下面整段复制到终端执行，会创建 `/etc/systemd/system/grab-odcr.service`）：

```bash
sudo tee /etc/systemd/system/grab-odcr.service > /dev/null <<'EOF'
[Unit]
Description=i4i ODCR capacity grabber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber
ExecStart=/usr/bin/python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

> 改命令参数就编辑这个文件的 `ExecStart=` 行；改完 `sudo systemctl daemon-reload && sudo systemctl restart grab-odcr`。
> `WorkingDirectory` / `User` 按你实际放脚本的路径和用户改。
> 这里**没写 `--target-cores`** 是对的：`--per-az-cores 5000` 会让总目标自动 = `5000 × 2 AZ = 10000`。启动后 `journalctl -u grab-odcr` 第一屏会有一行 `balanced mode: ... -> target 10000 vCPU`，确认是 10000 就对了。

**2. 启动 + 开机自启**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab-odcr      # 起 + 开机自启
sudo systemctl status grab-odcr            # 看是否 running
```

### 阶段二：大促中 —— 挂着不动，只看监控

脚本自己 `--watch` 死等抢货、抢满就停在那儿。**你什么都不用敲**，只偶尔看进度（见下方「监控」）。
真要改参数才动它：编辑服务文件后 `sudo systemctl daemon-reload && sudo systemctl restart grab-odcr`——`restart` 只是重启抢占进程，**不会释放已抢到的预留**（重启后按 AWS 实时持有续抢）。

### 阶段三：大促结束 —— 先停、再释放（这一步才停计费）

**这是两个独立动作，确认大促真的结束了再做：**

```bash
# (1) 先停 watcher（停止继续抢；已抢到的预留仍在、仍计费）
sudo systemctl stop grab-odcr
sudo systemctl disable grab-odcr           # 取消开机自启，防止重启又拉起来

# (2) 确认无误后，再释放全部预留 —— 这一步才真正停止计费（最关键的止血）
python3 grab_odcr.py --cancel-all --live
```

> 顺序不能反：先 `stop` 防止「边释放边又抢回来」，再 `cancel-all` 把预留全部释放。
> 跑完用 `python3 grab_odcr.py --list` 确认预留已清零、不再计费。

### 最小 IAM 权限

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeInstanceTypes",
      "ec2:CreateCapacityReservation",
      "ec2:DescribeCapacityReservations",
      "ec2:CancelCapacityReservation",
      "ec2:CreateTags"
    ],
    "Resource": "*"
  }]
}
```

> `ec2:DescribeInstanceTypes` 是给 `--types` 指定**非 i4i/i4g 机型**时用的：脚本会自动问 AWS
> 该机型有多少 vCPU（见下方「指定任意机型」）。只抢默认 i4i.16xlarge 时不会调用它，但建议保留——
> 缺了它，指定新机型当场报 `UnauthorizedOperation`。

---

## 监控：看抢了多少

三个出口，各回答一个不同的问题——**平时只用第 ①个就够**，②③ 是排查/对账才用：

| 想知道 | 用哪个 | 数据来自 |
|--------|--------|----------|
| ① **现在总共抢了多少核、每个 AZ 多少**（最常看） | `python3 grab_odcr.py --list` | 实时问 AWS |
| ② **脚本此刻在干啥、为啥没抢到**（排查用） | `journalctl` 或 `tail` 看运行日志 | 脚本运行流水 |
| ③ **每一笔抢占的明细**（事后对账用） | 读 `logs/grabs.jsonl` 台账 | 脚本写的本地文件 |

### ① 抢到多少 —— 最权威，直接问 AWS

```bash
python3 grab_odcr.py --list
```

输出 = 每条预留 + 一个汇总块（这就是「抢到哪了」的标准答案，按**核数**算、只统计本脚本 tag 的预留）。**summary 自动带进度**——`--list` 会从 `grabs.jsonl` 读出你上次抢的目标，显示 `已抢 / 目标` + `FULL/short`，不用再手敲 `--target-cores`：

```
cr-0aaa...  i4i.16xlarge  us-east-1b  active  count=78  tag=primeday-i4i-grab
cr-0bbb...  i4i.16xlarge  us-east-1d  active  count=78  tag=primeday-i4i-grab
--- summary (tag=primeday-i4i-grab) ---
  us-east-1b    4992 / 5000 vCPU  (78 x i4i.16xlarge) [short]
  us-east-1d    4992 / 5000 vCPU  (78 x i4i.16xlarge) [short]
  TOTAL         9984 / 10000 vCPU  across 2 AZ(s) [short]
```

**想持续盯进度**（每 30 秒自动刷新，`Ctrl+C` 停）——用系统 `watch` 包一层即可，脚本本身不用改：

```bash
watch -n 30 'python3 grab_odcr.py --list'
```

> 台账里没目标（全新环境、还没抢过）时，summary 退回纯数字（不带 `/ 目标` 和 `FULL/short`）。

### ② 脚本此刻在干啥 —— 运行日志

```bash
journalctl -u grab-odcr -f          # 用 systemd 跑的，看这个
tail -f logs/grab_odcr.log          # 直接 / tmux 跑的，看这个（脚本目录下）
```

> **这两条看的是同一份日志的两个出口**，内容一样，按你怎么跑脚本二选一：
> 脚本同时把日志吐到「控制台」和「文件 `logs/grab_odcr.log`」。systemd 跑时控制台被它接管、只能用 `journalctl` 看（机器重启后历史还在）；直接跑时没有 systemd unit，就 `tail` 文件。
> 内容：每轮扫了哪些 AZ、抢到/没抢到、限流退避、`have X/10000 vCPU` 进度。

### ③ 抢占明细台账 —— 对账用

`logs/grabs.jsonl` 是**机器可读的流水账**：每真正抢到一个预留，就追加一行 JSON。它和 ① 的区别是——① 给你「此刻的总数」，③ 给你「每一笔什么时候抢到的、攒的过程」，适合事后对账、画曲线、喂给别的工具。dry-run 不写，所以这里只会有真实抢占。

```bash
wc -l logs/grabs.jsonl                       # 一共抢到多少笔（每笔一行）
tail -n 5 logs/grabs.jsonl                   # 看最近 5 笔（别用 cat，几百行糊一屏）
tail -n 1 logs/grabs.jsonl | python3 -m json.tool   # 把最新一笔格式化看清字段
```

一行长这样（`total_vcpu` = 抢到这笔时的累计核数）：

```json
{"ts":"2026-06-13T07:12:16Z","via":"odcr","instance_type":"i4i.16xlarge","az":"us-east-1b","region":"us-east-1","vcpu":64,"total_vcpu":64,"target_vcpu":10000}
```

---

## 要知道的几件事

- **ODCR 不插队**：它和普通 On-Demand 抢同一个池子，没优先级。价值只在「抢到后停了也不还回去」。
- **能不能抢到是另一回事**：配额提够 ≠ 立刻抢到 10000 核。产能靠 AWS 间歇放出，靠 `--watch` 死等攒。
- **默认只抢 `i4i.16xlarge`（64 核）**：一台一大块，调用最少。要降级兜底加 `--types i4i.16xlarge i4i.8xlarge ...`（脚本自动按大到小排序）。
- **可指定任意机型**：`--types` 不再局限 i4i/i4g。传任何 EC2 机型，脚本自动问 AWS 拿它的 vCPU 数（`DescribeInstanceTypes`），见下方「指定任意机型」。
- **日志**：`logs/grab_odcr.log`（人读流水，自动轮转）、`logs/grabs.jsonl`（每抢到一个追加一行 JSON，对账用）。dry-run 不写台账。
- **成本**：`i4i.16xlarge` ≈ $5.491/小时·台（us-east-1）。实测 6 台跑 3 分钟约 $1.70。

---

## 指定任意机型

脚本不再只能抢 i4i/i4g。`--types` 可以传**任何** EC2 机型，每个机型的 vCPU 数用来算「抢了多少核 / per-AZ 上限 / 总目标」。来源分两种：

- **内置表里有的**（i4i / i4g 各档）：直接用，零额外调用。
- **内置表里没有的**（如 `r7i.48xlarge`、`m7i.24xlarge`、`c7i.metal-48xl`）：脚本启动时自动调一次 `ec2:DescribeInstanceTypes` 问 AWS 拿 `DefaultVCpus`，写进内存表，后续计数/扫描逻辑原样复用。

```bash
# 抢 r7i.48xlarge（192 vCPU/台），两个 AZ 各 5 台，先 dry-run
python3 grab_odcr.py --types r7i.48xlarge \
  --azs us-east-1b us-east-1d --per-az-cores 960

# 混合机型降级：先抢大的，抢不到落到小的（自动按 vCPU 大→小排序）
python3 grab_odcr.py --types r7i.48xlarge r7i.24xlarge r7i.12xlarge \
  --target-cores 3840 --live --watch
```

要点：

- **多一个只读权限**：`ec2:DescribeInstanceTypes`（已加进上面的最小 IAM）。缺它，指定新机型会当场报 `UnauthorizedOperation`。
- **AWS 不认识的机型名**会被丢弃并打印 warning；全被丢光则直接退出，不会乱抢。
- **`--list` 也认得**：之前用自定义机型抢的预留，`--list` 会自动补查它的 vCPU 再汇总，重启/换机器后核数不会少算。
- **不改代码加新机型**：以前要抢非 i4i（如 g7e）得整份复制脚本+硬编码 vCPU 表（见 `g7e/`）；现在直接 `--types` 即可，无需 fork。

---

## 按机型分别抢（`--per-type`）

默认模式是**凑总核数**：把多个机型当成降级链，朝一个共享的 `--target-cores` 抢，抢够就停，不保证每种各抢到多少。

`--per-type` 是另一套语义：**每种机型一个独立的「台数」目标，各抢各的**。适合你明确知道「16xl 要 10 台、8xl 要 5 台、r7i.24xl 要 3 台」这种按机型分配的需求。

```bash
# 先 dry-run 看计划
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 r7i.24xlarge:3

# 实弹 + 24×7 死等，直到每种都抢够各自的台数
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 --live --watch --interval 30

# 锁 AZ：在 1b/1d 里凑够各机型的总台数
python3 grab_odcr.py --per-type i4i.16xlarge:10 --azs us-east-1b us-east-1d --live --watch
```

要点：

- **格式** `TYPE:COUNT`：`COUNT` 是**台数**（不是核数），必须正整数。可传任意多个，重复机型以最后一个为准。格式错（缺冒号、非整数、≤0）直接退出（exit 2），不乱抢。
- **各抢各的、互不拖累**：某种机型全程没货，**不会**卡住其他机型——它被跳过，其他继续抢。没有共享核数目标。
- **AZ 是「总量在指定 AZ 里凑够」**：`i4i.16xlarge:10 --azs 1b 1d` = 16xl **一共** 10 台，1b/1d 哪个有货抢哪个，**不保证每 AZ 均分**。不传 `--azs` 就用 region 全部 AZ。**per-type 模式不做 per-AZ 均衡**（要均衡请用默认的 `--per-az-cores` 模式）。
- **覆盖旧参数**：设了 `--per-type` 时，`--target-cores` / `--per-az-cores` 被忽略并打印 warning。
- **任意机型**：非 i4i/i4g 机型同样自动查 vCPU（复用 `ec2:DescribeInstanceTypes`），AWS 不认识的机型丢弃并 warning。
- **重启幂等**：每轮从 AWS 重读各机型已持有台数，只补差额，崩溃/重启后续跑不会重复抢。
- **台账**：`grabs.jsonl` 仍按核数记录（`total_vcpu` = 该机型当前持有核数），`--list` 汇总格式不变。

---

## 仓库文件

| 文件 | 用途 |
|------|------|
| `grab_odcr.py` | 抢占脚本（唯一） |
| `common.py` | 共享工具：AZ 发现、退避重试、核数计数、日志台账 |
| `test_common.py` / `test_grab_odcr.py` | 单元测试（mock boto3，无 AWS）：`python3 -m unittest test_common test_grab_odcr` |
| [`扩大.md`](扩大.md) | **抢前必读**：配额清单，最该先提 `L-1216C47A` |
| [`客户配置.md`](客户配置.md) | **客户必读**：ASG / EKS 怎么配才能吃进预留 |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | t3.micro 验机制 + 真 i4i 验分布两份报告 |
| [`SMOKE_TEST_EKS.md`](SMOKE_TEST_EKS.md) | 独立 EKS self-managed nodegroup 端到端 runbook（与生产隔离） |
