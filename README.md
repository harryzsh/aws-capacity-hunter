# AWS Capacity Hunter

一个脚本，24×7 死等抢 EC2 容量，给 Prime Day 等大促囤产能。
**按机型报台数**（如 `i4i.16xlarge:100`，任何 EC2 机型都行），抢的是 **On-Demand 容量预留（ODCR）**：产能锁在你名下，实例停了也不丢，配合客户 ASG 自动吸纳。

```bash
# 演练（不花钱，先看计划）→ 实弹（⚠️ 立刻计费）
python3 grab_odcr.py --per-type i4i.16xlarge:156 --azs us-east-1b us-east-1d
python3 grab_odcr.py --per-type i4i.16xlarge:156 --azs us-east-1b us-east-1d --live --watch --interval 30
```

> 上面这条就是本次 Prime Day 的标准命令：在 1b/1d 两个 AZ 里抢够 **156 台 i4i.16xlarge**（= 10000 vCPU 量级）。

---

## 30 秒了解

![ODCR Capacity Grabber 流程图](docs/flow.svg)

一句话读图：`--watch` 每轮先**从 AWS 重读各机型真实持有台数**（崩溃/重启安全的根基），对还没抢够的机型逐个 (机型 × AZ) 组合抢占——**已有预留对象就 `Modify +1` 加量，没有才 `Create` 新建**，没货跳下一个组合、限流退避重试，抢到就记账，每种机型抢够各自的台数就停。

- **怎么报需求**：`--per-type TYPE:COUNT ...`，每种机型一个**独立台数目标**，各抢各的——某种没货绝不拖累其他机型。任何 EC2 机型都行（启动时自动向 AWS 验证机型名）。
- **怎么抢**：一次抢 1 台（全有或全无，+1 能扫到零星放出的碎片容量）；产能是间歇放出来的，所以 `--watch` 死等。
- **预留不碎片化**：每个「机型 × AZ」只保留**一个预留对象**，count 随抢占 +1 增长。抢满 156 台也只有 2 个对象，控制台清爽。详见[「预留合并机制与安全红线」](#预留合并机制与安全红线)。
- **怎么用上**：客户 EKS 的 ASG 设 `capacity-reservations-first`，扩容时实例自动落进预留。**脚本不碰客户 ASG**。
- **三件事必须先做对**（否则白忙）：
  1. **抢预留的账号 = EKS 集群账号**（open 预留只在同账号内匹配）。
  2. **vCPU 配额提够**（默认才 5，是头号阻塞，要提前开工单）→ 见 [`扩大.md`](扩大.md)。
  3. **客户 ASG 配好** → 见 [`客户配置.md`](客户配置.md)。

> 流程图源在 [`docs/flow.workflow.json`](docs/flow.workflow.json)，交互版（切深浅色、导出 PNG/SVG）在 [`docs/flow.html`](docs/flow.html)。

---

## 预留合并机制与安全红线

**这一节是全文最重要的安全须知，动预留之前先读完。**

### 机制：grow-or-create

每次抢到一台时：该 (机型 × AZ) **已有**我们 tag 的 active 预留 → `ModifyCapacityReservation` 把它的 count **+1**；**没有** → `CreateCapacityReservation` 新建 count=1。Modify 和 Create 的容量语义完全一样（需要池子里有空位、全有或全无），所以**抢占粒度和成功率不变**，只是对象数收敛：每个机型 × AZ 恒为一个对象。

### 红线一：单写者约束 ⚠️

`ModifyCapacityReservation` 传的是**绝对台数**（不是「+1」增量）。脚本按「每轮从 AWS 重读真值 + 轮内本地累加」计算下一个数，所以对**同一 region 里本脚本 tag 的预留，写者必须只有一个**。满足这一条，数量只增不减、数学上不可能变少。

| 场景 | 安全？ |
|------|--------|
| 单个 systemd unit 跑 `--live --watch` | ✅ 设计场景 |
| 进程崩溃 / 机器重启后续跑（新旧进程时间不重叠） | ✅ 从 AWS 真值续抢 |
| 脚本跑着，另开终端 `--list` / 控制台**只看** | ✅ 只读随便看 |
| 另一份脚本抢**不同 region** | ✅ 预留是 region 级资源，互不相干 |
| 脚本跑着，控制台/CLI **手动改**预留的 count | ❌ 你加的量可能被脚本下一次写回覆盖、释放回公共池 |
| 两台机器 / 两个进程同时 `--live` 抢同一 region | ❌ 互相覆盖对方增量 |

大促中想临时加量：**别在控制台点**——两种安全做法任选：
① 改 systemd 的 `--per-type` 绝对目标再 `restart`（不释放已持有）；
② 先 `systemctl stop`，跑一次性加抢 `python3 grab_odcr.py --per-type TYPE:N --add --live`（在现有持有量上再抢 N 台），完了再 `systemctl start`。`--add` 拒绝和 `--watch` 同用，防止重启循环无限加抢。

### 红线二：炸弹半径

合并后一个对象承载一个 AZ 的**全部**容量，误取消一个就全丢。脚本自己只在 `--cancel-all` 里取消；**绝不要在控制台手动取消单个预留**。

### 统计口径 vs 修改边界（重要区分）

**「算进目标」和「往上面加量/取消」是两回事：**

- **统计（只读）**：默认把该机型在目标 AZ 里的**所有** active 预留都算进「已持有」——包括 AWS 协助（`AWS Assisted`）建的、控制台手动建的、别的工具建的。所以目标就是账号级总量：账号里已有 133 台、目标 135 → 只补抢 2 台。加 `--only-mine` 切回「只算本脚本 tag 的」旧口径。
- **修改/取消（写操作）**：**永远只碰本脚本 tag 的预留**。非本脚本的预留绝不会被 Modify（它们可能有别的写者在管，绝对值写入会覆盖别人的增量）、也绝不会被 `--cancel-all` 释放。所以补抢的量会落在脚本**自己**的预留对象上（没有就新建一个），不会长在别人的对象上。

### 边界情况：同 (机型×AZ) 已有多个本脚本旧预留

旧版脚本抢的 count=1 碎片还在时：新容量只会挑其中一个持续 +1，**其余原样保留、不动也不合并**——合并要先 cancel 再加量，中间容量会回公共池被人抢走，大促期间绝不做。进度统计按全部对象的台数加总，不重抢不少算。想收敛到一个对象：等大促结束 `--cancel-all` 清零后重抢。

### 附带语义

- **预留没有到期时间**（`EndDateType=unlimited`）：抢到就一直持有、一直计费，直到你 `--cancel-all` 释放。**大促结束务必记得释放**。
- **数量主动减少的唯一入口**自始至终只有 `--cancel-all --live`，是大促后你手动执行的止血动作。

---

## 三步上手

**1. 装**

```bash
pip install -r requirements.txt   # 只需 boto3，Python 3.8+
```

凭证用环境变量 / `~/.aws/credentials` / IAM role 都行。在 EC2 上长跑推荐 **instance profile**。

**2. 演练**（默认就是 dry-run，不加 `--live` 不花一分钱）

```bash
python3 grab_odcr.py --per-type i4i.16xlarge:156 --azs us-east-1b us-east-1d
```

演练把「能不能跑通」全验一遍，唯独不真建、不花钱：

- ✅ **真连 AWS**：只读 API 验证机型名、列 AZ、查供货——凭证错、权限不够、账号/区域不对、机型名打错会当场报错。
- ✅ **真算计划**：各机型抢几台、在哪些 AZ，打印出来给你核对。
- ✅ **真试建预留、但带 `DryRun` 标志**：AWS 校验参数和权限后拒绝真建——**连「有没有权限建预留」都验到了**。
- ❌ **不建任何预留、不计费、不写台账**（`grabs.jsonl` 只记真实抢占）。

> 一句话：演练 = 权限、账号、机型、供货、计划数字全验一遍，就差没按扣费键。看着没问题，再加 `--live`。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

```bash
python3 grab_odcr.py --per-type i4i.16xlarge:156 --azs us-east-1b us-east-1d --live --watch --interval 30
```

挂在 systemd 里 24×7 跑（见下）。每种机型抢够各自的台数它自己停。**用完一定要释放，停止计费：**

```bash
python3 grab_odcr.py --cancel-all --live
```

---

## 常用命令与参数

```bash
python3 grab_odcr.py --list                          # 看各机型抢了多少台（自动带目标进度）
watch -n 30 'python3 grab_odcr.py --list'            # 持续盯进度，每 30 秒刷新，Ctrl+C 停
python3 grab_odcr.py --per-type i4i.16xlarge:100 i4i.8xlarge:50 --live --watch --interval 30
python3 grab_odcr.py --cancel-all --live             # 释放全部、停止计费
```

| 参数 | 作用 |
|------|------|
| `--per-type TYPE:COUNT ...` | **要抢什么、各抢几台**（唯一的抢占入口）。COUNT 是**账号级持有目标**：统计该机型在目标 AZ 里的**全部** active 预留（含 AWS 协助建的、手动建的等非本脚本预留——只计数、绝不修改），已持有 ≥ COUNT 就不再抢。每种机型一个独立目标，各抢各的。COUNT 是台数、正整数；格式错直接退出（exit 2）。任何 EC2 机型都行，启动时自动向 AWS 验证 |
| `--only-mine` | 切回旧口径：**只统计本脚本 tag 的预留**。此时 COUNT 表示「本脚本要抢到 N 台」，账号里其他来源的存量不算 |
| `--add` | **一次性手动加抢**：COUNT 变成「在现有持有量上**再抢 N 台**」（如已持有 24，`--per-type i7i.8xlarge:1 --add --live` → 抢到 25）。必须配 `--live`；**禁止配 `--watch`**（重启的 watcher 会无限加抢，脚本直接拒绝这个组合） |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d`。COUNT 是**在这些 AZ 里凑够的总台数**，哪个 AZ 有货抢哪个，不做 per-AZ 均分。**已持有量也只统计这些 AZ**——别的 AZ 里的存量不会顶掉目标（如 1a 已有 24 台，`--azs us-east-1b ...:5` 仍会在 1b 抢满 5 台）。不传 = region 全部 AZ |
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能间歇放出，必须死等） |
| `--region R` | 区域，默认 `us-east-1` |
| `--cancel-all` | 取消全部预留、停止计费 |
| `--list` | 只看不动（每条预留 + 各机型台数汇总，自动从台账读目标显示进度） |

完整参数 `python3 grab_odcr.py --help`。

> **重启幂等**：目标按「AWS 实时持有的台数」判断（不是进程内存），崩溃/重启后续跑只补差额，不超抢、也不会在旧对象旁边开新对象。配 systemd `Restart=always` 安全。

---

## 24×7 在 EC2 上跑（systemd）

开一台 `t3.micro`（脚本几乎不耗资源），用 instance profile 拿凭证，丢给 systemd 长跑——断了自动拉起、机器重启自启。大促期间脚本**一直挂着死等**，你只需偶尔看进度（见下方「监控」）。

> **三个阶段，三个不同时间点，别搞混**：
> **大促前**部署就位 → **大促中**挂着不动、只看监控 → **大促后**先 `stop` 再 `cancel-all`。
> ⚠️ **`stop` 和 `cancel-all` 是两码事**：`stop` 只停止「继续抢」，**已抢到的预留还在、还在计费**；只有 `cancel-all` 才真正释放预留、停止计费。这俩**绝不能串成一条命令**——大促期间你可能会 `stop`（比如改配置重启），但**绝不能 cancel**。

### 阶段一：大促前 —— 部署就位

**1. 新建 systemd 服务文件**（整段复制到终端执行）：

```bash
sudo tee /etc/systemd/system/grab-odcr.service > /dev/null <<'EOF'
[Unit]
Description=ODCR capacity grabber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/aws-capacity-hunter
ExecStart=/usr/bin/python3 grab_odcr.py --per-type i4i.16xlarge:100 i4i.8xlarge:50 --azs us-east-1b us-east-1d --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

> ⚠️ `--per-type i4i.16xlarge:100 i4i.8xlarge:50` 是**占位示例值**，按实际要抢的机型和台数改（`TYPE:COUNT`，COUNT 是台数）。
> 改参数就编辑 `ExecStart=` 行，改完 `sudo systemctl daemon-reload && sudo systemctl restart grab-odcr`。`WorkingDirectory` / `User` 按实际路径和用户改。
> 启动后看 `journalctl -u grab-odcr` 第一屏，核对打印的 `targets={...} AZs=[...]` 是否正确。

**2. 启动 + 开机自启**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab-odcr      # 起 + 开机自启
sudo systemctl status grab-odcr            # 看是否 running
```

### 阶段二：大促中 —— 挂着不动，只看监控

脚本自己死等抢货、抢满就停在那儿。**你什么都不用敲**，只偶尔看进度。
真要改参数才动它：编辑服务文件后 `daemon-reload && restart`——`restart` 只是重启抢占进程，**不会释放已抢到的预留**。

> ⚠️ 运行期间**不要在控制台/CLI 手动改这些预留的台数、不要起第二份脚本抢同一 region**（[单写者约束](#红线一单写者约束-)）。看（控制台浏览、`--list`）随便看。

### 阶段三：大促结束 —— 先停、再释放（这一步才停计费）

```bash
# (1) 先停 watcher（停止继续抢；已抢到的预留仍在、仍计费）
sudo systemctl stop grab-odcr
sudo systemctl disable grab-odcr           # 取消开机自启，防止重启又拉起来

# (2) 确认无误后，再释放全部预留 —— 这一步才真正停止计费（最关键的止血）
python3 grab_odcr.py --cancel-all --live
```

> 顺序不能反：先 `stop` 防止「边释放边又抢回来」，再 `cancel-all`。
> 跑完用 `--list` 确认预留已清零、不再计费。

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
      "ec2:ModifyCapacityReservation",
      "ec2:DescribeCapacityReservations",
      "ec2:CancelCapacityReservation",
      "ec2:CreateTags"
    ],
    "Resource": "*"
  }]
}
```

> `ec2:ModifyCapacityReservation` 是合并加量用的，**缺了它第二台起全部报错**。
> `ec2:DescribeInstanceTypes` 是启动时验证非 i4i/i4g 机型名用的，只抢 i4i/i4g 不会调用，但建议保留。

---

## 监控：看抢了多少

三个出口，各答一个问题——**平时只用第 ① 个就够**，②③ 排查/对账才用：

| 想知道 | 用哪个 | 数据来自 |
|--------|--------|----------|
| ① **各机型现在抢了多少台**（最常看） | `python3 grab_odcr.py --list` | 实时问 AWS |
| ② **脚本此刻在干啥、为啥没抢到**（排查用） | `journalctl` 或 `tail` 看运行日志 | 脚本运行流水 |
| ③ **每一笔抢占的明细**（事后对账用） | 读 `logs/grabs.jsonl` 台账 | 脚本写的本地文件 |

### ① 抢到多少 —— 最权威，直接问 AWS

```bash
python3 grab_odcr.py --list
watch -n 30 'python3 grab_odcr.py --list'    # 持续盯，每 30 秒刷新
```

每个「机型 × AZ」一个预留对象、count 随抢占增长，所以列表始终就这么几行。summary 按**机型台数**汇总，自动从台账读上次的目标，显示 `已抢 / 目标` + `FULL/short`：

```
cr-0aaa...  i4i.16xlarge  us-east-1b  active  count=78  tag=primeday-i4i-grab
cr-0bbb...  i4i.16xlarge  us-east-1d  active  count=78  tag=primeday-i4i-grab
--- summary (tag=primeday-i4i-grab) ---
  i4i.16xlarge    156 / 156 instances [FULL]
  TOTAL           156 instances  across 1 type(s)
  USED            2 / 2 reservations USED (have an instance running)
```

> 台账里没目标（全新环境）时，summary 退回纯台数（不带 `/ 目标` 和 `FULL/short`）。

### ② 脚本此刻在干啥 —— 运行日志

```bash
journalctl -u grab-odcr -f          # systemd 跑的看这个（重启后历史还在）
tail -f logs/grab_odcr.log          # 直接 / tmux 跑的看这个
```

两条是同一份日志的两个出口，内容一样，按跑法二选一。内容：每轮扫了哪些 AZ、抢到/没抢到、限流退避、`have {...}` 各机型进度。

### ③ 抢占明细台账 —— 对账用

`logs/grabs.jsonl`：每真正抢到一台追加一行 JSON（dry-run 不写）。① 给「此刻总数」，③ 给「每一笔的时间线」，适合对账、画曲线、喂工具。

```bash
wc -l logs/grabs.jsonl                              # 一共抢到多少笔
tail -n 5 logs/grabs.jsonl                          # 最近 5 笔
tail -n 1 logs/grabs.jsonl | python3 -m json.tool   # 最新一笔格式化
```

```json
{"ts":"2026-06-13T07:12:16Z","via":"odcr","instance_type":"i4i.16xlarge","az":"us-east-1b","region":"us-east-1","held_count":78,"target_count":156}
```

---

## 要知道的几件事

- **ODCR 不插队**：它和普通 On-Demand 抢同一个池子，没优先级。价值只在「抢到后停了也不还回去」。
- **能不能抢到是另一回事**：配额提够 ≠ 立刻抢到。产能靠 AWS 间歇放出，靠 `--watch` 死等攒。
- **任意机型都能抢**：`--per-type` 里写任何 EC2 机型（如 `r7i.48xlarge:5`），启动时自动向 AWS 验证机型名，不认识的丢弃并 warning，全被丢光则直接退出、不乱抢。
- **日志**：`logs/grab_odcr.log`（人读流水，自动轮转）、`logs/grabs.jsonl`（对账台账）。dry-run 不写台账。
- **成本**：`i4i.16xlarge` ≈ $5.491/小时·台（us-east-1）。实测 6 台跑 3 分钟约 $1.70。

（预留合并、单写者、炸弹半径等安全事项见上方[「预留合并机制与安全红线」](#预留合并机制与安全红线)。）

---

## 仓库文件

| 文件 | 用途 |
|------|------|
| `grab_odcr.py` | 抢占脚本（唯一） |
| `common.py` | 共享工具：AZ 发现、机型验证、退避重试、日志台账 |
| `docs/flow.svg` / [`docs/flow.html`](docs/flow.html) | 运行流程图（SVG 嵌 README；HTML 为交互版，图源 `docs/flow.workflow.json`） |
| `test_common.py` / `test_grab_odcr.py` | 单元测试（mock boto3，无 AWS）：`python3 -m unittest test_common test_grab_odcr` |
| [`扩大.md`](扩大.md) | **抢前必读**：配额清单，最该先提 `L-1216C47A` |
| [`客户配置.md`](客户配置.md) | **客户必读**：ASG / EKS 怎么配才能吃进预留 |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | t3.micro 验机制 + 真 i4i 验分布两份报告 |
| [`SMOKE_TEST_EKS.md`](SMOKE_TEST_EKS.md) | 独立 EKS self-managed nodegroup 端到端 runbook（与生产隔离） |
