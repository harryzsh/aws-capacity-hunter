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

![ODCR Capacity Grabber 流程图](docs/flow.svg)

一句话读图：`--watch` 每轮先**从 AWS 重读真实持有核数**（崩溃/重启安全的根基），闸门算出差额后逐个 (机型 × AZ) 组合抢占——**已有预留对象就 `Modify +1` 加量，没有才 `Create` 新建**，没货跳下一个组合、限流退避重试，抢到就记账，抢满自停。

- **目标**：抢 10000 vCPU 的 i4i，均匀铺在 `us-east-1b` / `us-east-1d` 两个 AZ。
- **怎么抢**：每次抢 1 台 `i4i.16xlarge`（64 核）的 open 预留容量，累加到目标；产能是间歇放出来的，所以 `--watch` 死等。
- **预留不碎片化**：每个「机型 × AZ」只保留**一个预留对象**，count 随抢占 +1 增长。抢满 156 台也只有 2 个对象，控制台清爽。详见下方[「预留合并机制与安全红线」](#预留合并机制与安全红线)。
- **怎么用上**：客户 EKS 的 ASG 设 `capacity-reservations-first`，扩容时实例自动落进预留。**脚本不碰客户 ASG**。
- **三件事必须先做对**（否则白忙）：
  1. **抢预留的账号 = EKS 集群账号**（open 预留只在同账号内匹配）。
  2. **vCPU 配额提到 ≥ 12000**（默认才 5，是头号阻塞，要提前开工单）→ 见 [`扩大.md`](扩大.md)。
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

大促中想临时加量：**别在控制台点**——改 systemd 的目标参数让脚本自己抢（`restart` 不释放已持有），或先 `systemctl stop` 再手动操作。

### 红线二：炸弹半径

合并后一个对象承载一个 AZ 的**全部**容量，误取消一个就全丢。脚本自己只在 `--cancel-all` 里取消；**绝不要在控制台手动取消单个预留**。

### 边界情况：同 (机型×AZ) 已有多个旧预留

旧版脚本抢的 count=1 碎片、或手动建的预留还在时：新容量只会挑其中一个持续 +1，**其余原样保留、不动也不合并**——合并要先 cancel 再加量，中间容量会回公共池被人抢走，大促期间绝不做。进度统计按全部对象的核数加总，不重抢不少算。想收敛到一个对象：等大促结束 `--cancel-all` 清零后重抢。

### 附带语义

- **`--end-hours` 作用在预留对象上**：过期时间在对象创建时设定，之后 +1 长进来的容量跟同一个到期时间走（整个对象一起过期）。
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
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000
```

演练把「能不能跑通」全验一遍，唯独不真建、不花钱：

- ✅ **真连 AWS**：只读 API 列 AZ、查供货——凭证错、权限不够、账号/区域不对会当场报错。
- ✅ **真算计划**：`--per-az-cores 5000` × 2 AZ → 总目标 10000，打印出来给你核对。
- ✅ **真试建预留、但带 `DryRun` 标志**：AWS 校验参数和权限后拒绝真建——**连「有没有权限建预留」都验到了**。
- ❌ **不建任何预留、不计费、不写台账**（`grabs.jsonl` 只记真实抢占）。

> 一句话：演练 = 权限、账号、供货、计划数字全验一遍，就差没按扣费键。看着没问题，再加 `--live`。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

```bash
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
```

挂在 systemd 里 24×7 跑（见下）。抢够了它自己停。**用完一定要释放，停止计费：**

```bash
python3 grab_odcr.py --cancel-all --live
```

---

## 常用命令与参数

```bash
python3 grab_odcr.py --list                          # 看当前抢了多少（per-AZ + 总计，自动带目标进度）
watch -n 30 'python3 grab_odcr.py --list'            # 持续盯进度，每 30 秒刷新，Ctrl+C 停
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30   # 按总核数抢（不分 AZ）
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 --live --watch   # 按机型分别抢各自台数
python3 grab_odcr.py ... --live --end-hours 6        # 计费保险：6 小时后预留自动过期
python3 grab_odcr.py --cancel-all --live             # 释放全部、停止计费
```

| 参数 | 作用 |
|------|------|
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能间歇放出，必须死等） |
| `--per-az-cores N` | 每 AZ 各封顶 N 核，均衡铺货（对齐 ASG 50/50）。**客户每 AZ 一个 ASG 时务必用**。设了它就不用再写 `--target-cores`：总目标自动 = `N × AZ数`，启动日志打印 `balanced mode: ...` 确认 |
| `--target-cores N` | 总共抢够 N 核就停。字面默认 8（占位），设了 `--per-az-cores` 会被自动覆盖成 `N × AZ数`。⚠️ 两个都写且对不上时以 `--target-cores` 为硬总闸——要么只写 `--per-az-cores`，要么保证 `target = per_az × AZ数` |
| `--per-type TYPE:COUNT ...` | 按机型分别抢：每种一个**独立台数目标**，各抢各的，某种没货不拖累其他。覆盖 `--target-cores`/`--per-az-cores`。见[「按机型分别抢」](#按机型分别抢--per-type) |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d` |
| `--region R` | 区域，默认 `us-east-1` |
| `--cancel-all` | 取消全部预留、停止计费 |
| `--list` | 只看不动（每条预留 + per-AZ/总计汇总，自动从台账读目标显示进度） |

完整参数 `python3 grab_odcr.py --help`。

> **重启幂等**：上限按「AWS 实时持有的核数」判断（不是进程内存），崩溃/重启后续跑只补差额，不超抢、不抢歪、也不会在旧对象旁边开新对象。配 systemd `Restart=always` 安全。

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
Description=i4i ODCR capacity grabber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber
ExecStart=/usr/bin/python3 grab_odcr.py --per-type i4i.16xlarge:100 i4i.8xlarge:50 --azs us-east-1b us-east-1d --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

> ⚠️ `--per-type i4i.16xlarge:100 i4i.8xlarge:50` 是**占位示例值**，按实际要抢的机型和台数改（`TYPE:COUNT`，COUNT 是台数）。也可换按核数均衡的写法 `--per-az-cores 5000`——两种模式二选一，不要同时写。
> 改参数就编辑 `ExecStart=` 行，改完 `sudo systemctl daemon-reload && sudo systemctl restart grab-odcr`。`WorkingDirectory` / `User` 按实际路径和用户改。
> 启动后看 `journalctl -u grab-odcr` 第一屏，核对打印的目标是否正确。

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
> `ec2:DescribeInstanceTypes` 是 `--types`/`--per-type` 指定非 i4i/i4g 机型时自动查 vCPU 用的，只抢默认机型不会调用，但建议保留。

---

## 监控：看抢了多少

三个出口，各答一个问题——**平时只用第 ① 个就够**，②③ 排查/对账才用：

| 想知道 | 用哪个 | 数据来自 |
|--------|--------|----------|
| ① **现在总共抢了多少核、每个 AZ 多少**（最常看） | `python3 grab_odcr.py --list` | 实时问 AWS |
| ② **脚本此刻在干啥、为啥没抢到**（排查用） | `journalctl` 或 `tail` 看运行日志 | 脚本运行流水 |
| ③ **每一笔抢占的明细**（事后对账用） | 读 `logs/grabs.jsonl` 台账 | 脚本写的本地文件 |

### ① 抢到多少 —— 最权威，直接问 AWS

```bash
python3 grab_odcr.py --list
watch -n 30 'python3 grab_odcr.py --list'    # 持续盯，每 30 秒刷新
```

每个「机型 × AZ」一个预留对象、count 随抢占增长，所以列表始终就这么几行。summary 自动从台账读上次的目标，显示 `已抢 / 目标` + `FULL/short`：

```
cr-0aaa...  i4i.16xlarge  us-east-1b  active  count=78  tag=primeday-i4i-grab
cr-0bbb...  i4i.16xlarge  us-east-1d  active  count=78  tag=primeday-i4i-grab
--- summary (tag=primeday-i4i-grab) ---
  us-east-1b    4992 / 5000 vCPU  (78 x i4i.16xlarge) [short]
  us-east-1d    4992 / 5000 vCPU  (78 x i4i.16xlarge) [short]
  TOTAL         9984 / 10000 vCPU  across 2 AZ(s) [short]
```

> 台账里没目标（全新环境）时，summary 退回纯数字（不带 `/ 目标` 和 `FULL/short`）。

### ② 脚本此刻在干啥 —— 运行日志

```bash
journalctl -u grab-odcr -f          # systemd 跑的看这个（重启后历史还在）
tail -f logs/grab_odcr.log          # 直接 / tmux 跑的看这个
```

两条是同一份日志的两个出口，内容一样，按跑法二选一。内容：每轮扫了哪些 AZ、抢到/没抢到、限流退避、`have X/10000 vCPU` 进度。

### ③ 抢占明细台账 —— 对账用

`logs/grabs.jsonl`：每真正抢到一台追加一行 JSON（dry-run 不写）。① 给「此刻总数」，③ 给「每一笔的时间线」，适合对账、画曲线、喂工具。

```bash
wc -l logs/grabs.jsonl                              # 一共抢到多少笔
tail -n 5 logs/grabs.jsonl                          # 最近 5 笔
tail -n 1 logs/grabs.jsonl | python3 -m json.tool   # 最新一笔格式化
```

```json
{"ts":"2026-06-13T07:12:16Z","via":"odcr","instance_type":"i4i.16xlarge","az":"us-east-1b","region":"us-east-1","vcpu":64,"total_vcpu":64,"target_vcpu":10000}
```

---

## 要知道的几件事

- **ODCR 不插队**：它和普通 On-Demand 抢同一个池子，没优先级。价值只在「抢到后停了也不还回去」。
- **能不能抢到是另一回事**：配额提够 ≠ 立刻抢到 10000 核。产能靠 AWS 间歇放出，靠 `--watch` 死等攒。
- **默认只抢 `i4i.16xlarge`（64 核）**：一台一大块，调用最少。要降级兜底加 `--types i4i.16xlarge i4i.8xlarge ...`（自动按大到小排序）。
- **日志**：`logs/grab_odcr.log`（人读流水，自动轮转）、`logs/grabs.jsonl`（对账台账）。dry-run 不写台账。
- **成本**：`i4i.16xlarge` ≈ $5.491/小时·台（us-east-1）。实测 6 台跑 3 分钟约 $1.70。

（预留合并、单写者、炸弹半径等安全事项见上方[「预留合并机制与安全红线」](#预留合并机制与安全红线)。）

---

## 指定任意机型

`--types` 可以传**任何** EC2 机型，vCPU 数用来算「抢了多少核 / per-AZ 上限 / 总目标」：

- **内置表里有的**（i4i / i4g 各档）：直接用，零额外调用。
- **内置表里没有的**（如 `r7i.48xlarge`、`c7i.metal-48xl`）：启动时自动调一次 `ec2:DescribeInstanceTypes` 拿 `DefaultVCpus`，后续逻辑原样复用。

```bash
# 抢 r7i.48xlarge（192 vCPU/台），两个 AZ 各 5 台，先 dry-run
python3 grab_odcr.py --types r7i.48xlarge \
  --azs us-east-1b us-east-1d --per-az-cores 960

# 混合机型降级：先抢大的，抢不到落到小的（自动按 vCPU 大→小排序）
python3 grab_odcr.py --types r7i.48xlarge r7i.24xlarge r7i.12xlarge \
  --target-cores 3840 --live --watch
```

要点：

- **AWS 不认识的机型名**会被丢弃并打印 warning；全被丢光则直接退出，不会乱抢。
- **`--list` 也认得**：自定义机型抢的预留会自动补查 vCPU 再汇总，重启/换机器后核数不会少算。
- **不改代码加新机型**：以前抢非 i4i 得整份复制脚本+硬编码 vCPU 表（见 `g7e/`）；现在直接 `--types` 即可。

---

## 按机型分别抢（`--per-type`）

默认模式是**凑总核数**：多个机型当降级链，朝共享的 `--target-cores` 抢，不保证每种各抢多少。
`--per-type` 是另一套语义：**每种机型一个独立「台数」目标，各抢各的**。适合「16xl 要 10 台、8xl 要 5 台」这种按机型分配的需求。

```bash
# 先 dry-run 看计划
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 r7i.24xlarge:3

# 实弹 + 24×7 死等，直到每种都抢够各自的台数
python3 grab_odcr.py --per-type i4i.16xlarge:10 i4i.8xlarge:5 --live --watch --interval 30

# 锁 AZ：在 1b/1d 里凑够各机型的总台数
python3 grab_odcr.py --per-type i4i.16xlarge:10 --azs us-east-1b us-east-1d --live --watch
```

要点：

- **格式** `TYPE:COUNT`：COUNT 是**台数**（不是核数），正整数。重复机型以最后一个为准。格式错直接退出（exit 2），不乱抢。
- **各抢各的、互不拖累**：某种机型没货只是被跳过，其他继续抢。没有共享核数目标。
- **AZ 是「总量在指定 AZ 里凑够」**：`i4i.16xlarge:10 --azs 1b 1d` = 一共 10 台，哪个 AZ 有货抢哪个，**不保证每 AZ 均分**（要均衡用默认的 `--per-az-cores` 模式）。
- **覆盖旧参数**：设了 `--per-type` 时 `--target-cores` / `--per-az-cores` 被忽略并打印 warning。
- **重启幂等 / 任意机型 / 台账**：与默认模式一致——每轮从 AWS 重读持有台数只补差额；非 i4i 机型自动查 vCPU；`grabs.jsonl` 仍按核数记录。

---

## 仓库文件

| 文件 | 用途 |
|------|------|
| `grab_odcr.py` | 抢占脚本（唯一） |
| `common.py` | 共享工具：AZ 发现、退避重试、核数计数、日志台账 |
| `docs/flow.svg` / [`docs/flow.html`](docs/flow.html) | 运行流程图（SVG 嵌 README；HTML 为交互版，图源 `docs/flow.workflow.json`） |
| `test_common.py` / `test_grab_odcr.py` | 单元测试（mock boto3，无 AWS）：`python3 -m unittest test_common test_grab_odcr` |
| [`扩大.md`](扩大.md) | **抢前必读**：配额清单，最该先提 `L-1216C47A` |
| [`客户配置.md`](客户配置.md) | **客户必读**：ASG / EKS 怎么配才能吃进预留 |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | t3.micro 验机制 + 真 i4i 验分布两份报告 |
| [`SMOKE_TEST_EKS.md`](SMOKE_TEST_EKS.md) | 独立 EKS self-managed nodegroup 端到端 runbook（与生产隔离） |
