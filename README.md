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
python3 grab_odcr.py --list                          # 看当前持有哪些预留
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30   # 按总核数抢（不分 AZ）
python3 grab_odcr.py ... --live --end-hours 6        # 计费保险：6 小时后预留自动过期
python3 grab_odcr.py --cancel-all --live             # 释放全部、停止计费
```

### 关键参数

| 参数 | 作用 |
|------|------|
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能是间歇放出来的，必须死等） |
| `--per-az-cores N` | 每个 AZ 各封顶 N 核，均衡铺货（对齐 ASG 50/50）。**客户每 AZ 一个 ASG 时务必用** |
| `--target-cores N` | 总共抢够 N 核就停。单设 `--per-az-cores` 时总目标自动 = N × AZ 数 |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d` |
| `--region R` | 区域，默认 `us-east-1` |
| `--cancel-all` | 取消全部预留、停止计费 |
| `--list` | 只看不动 |

完整参数 `python3 grab_odcr.py --help`。

> **重启幂等**：上限按「AWS 实时持有的核数」判断（不是进程内存），所以进程崩溃 / 机器重启后续跑，会按各 AZ 真实持有**只补差额**，不超抢、不抢歪。配 systemd `Restart=always` 安全。

---

## 24×7 在 EC2 上跑（systemd）

开一台 `t3.micro`（脚本几乎不耗资源），用 instance profile 拿凭证，丢给 systemd 长跑——断了自动拉起、机器重启自启。

`/etc/systemd/system/grab-odcr.service`：

```ini
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
```

```bash
sudo systemctl enable --now grab-odcr      # 起 + 开机自启
journalctl -u grab-odcr -f                 # 看日志
# 大促结束：
sudo systemctl stop grab-odcr && python3 grab_odcr.py --cancel-all --live
```

### 最小 IAM 权限

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:CreateCapacityReservation",
      "ec2:DescribeCapacityReservations",
      "ec2:CancelCapacityReservation",
      "ec2:CreateTags"
    ],
    "Resource": "*"
  }]
}
```

---

## 要知道的几件事

- **ODCR 不插队**：它和普通 On-Demand 抢同一个池子，没优先级。价值只在「抢到后停了也不还回去」。
- **能不能抢到是另一回事**：配额提够 ≠ 立刻抢到 10000 核。产能靠 AWS 间歇放出，靠 `--watch` 死等攒。
- **默认只抢 `i4i.16xlarge`（64 核）**：一台一大块，调用最少。要降级兜底加 `--types i4i.16xlarge i4i.8xlarge ...`（脚本自动按大到小排序）。
- **日志**：`logs/grab_odcr.log`（人读流水，自动轮转）、`logs/grabs.jsonl`（每抢到一个追加一行 JSON，对账用）。dry-run 不写台账。
- **成本**：`i4i.16xlarge` ≈ $5.491/小时·台（us-east-1）。实测 6 台跑 3 分钟约 $1.70。

---

## 仓库文件

| 文件 | 用途 |
|------|------|
| `grab_odcr.py` | 抢占脚本（唯一） |
| `common.py` | 共享工具：AZ 发现、退避重试、核数计数、日志台账 |
| `test_grab_odcr.py` | 单元测试（mock boto3，验重启幂等逻辑），`python3 -m unittest test_grab_odcr` |
| [`扩大.md`](扩大.md) | **抢前必读**：配额清单，最该先提 `L-1216C47A` |
| [`客户配置.md`](客户配置.md) | **客户必读**：ASG / EKS 怎么配才能吃进预留 |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | 两份端到端验证报告（机制 + 真 i4i 分布，已通过） |
