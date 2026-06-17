# ec2-g-capacity-grabber（g6e + g7e，按台数、多机型）

一个脚本，24×7 死等抢 **g6e.48xlarge / g7e.48xlarge** EC2 容量。抢的是 **On-Demand 容量预留（ODCR）**：产能锁在你名下，实例停了也不丢。

> 这是仓库根目录 i4i grabber 的 **按台数（instance count）、多机型** 版本。你指定**每种机型抢多少台**，还能给**每种机型在每个 AZ 指定不同的台数**。两种机型都是 .48xlarge（192 vCPU / 8 GPU），都属于 EC2 **G 系列**,共用同一个 G/VT 配额。

```bash
# 演练（不花钱）→ 实弹（⚠️ 立刻计费）
python3 grab_g7e_odcr.py --az-counts g7e.48xlarge@us-east-1b=5 g7e.48xlarge@us-east-1d=3 \
                         g6e.48xlarge@us-east-1b=2 g6e.48xlarge@us-east-1d=10
python3 grab_g7e_odcr.py --az-counts g7e.48xlarge@us-east-1b=5 g7e.48xlarge@us-east-1d=3 \
                         g6e.48xlarge@us-east-1b=2 g6e.48xlarge@us-east-1d=10 --live --watch --interval 30
```

## 三种指定目标的方式

内部都会编译成一个矩阵 `(机型, AZ) -> 台数`。

**A) 显式 per-(机型,AZ) —— 每个 AZ、每种机型台数都可不同（最灵活）**
```bash
python3 grab_g7e_odcr.py --az-counts \
  g7e.48xlarge@us-east-1b=5 g7e.48xlarge@us-east-1d=3 \
  g6e.48xlarge@us-east-1b=2 g6e.48xlarge@us-east-1d=10 --live --watch
```
AZ 直接从 `机型@AZ=台数` 的 key 里取,不用再写 `--azs`。

**B) 每种机型给总数,在 --azs 间均摊**
```bash
python3 grab_g7e_odcr.py --counts g6e.48xlarge=10 g7e.48xlarge=20 \
  --azs us-east-1b us-east-1d --balance --live --watch
```

**C) 每种机型给总数,贪婪铺（不限每 AZ）**
```bash
python3 grab_g7e_odcr.py --counts g6e.48xlarge=10 g7e.48xlarge=20 \
  --azs us-east-1b us-east-1d --live --watch
```

**（向后兼容：单机型 g7e 老写法仍可用）**
```bash
python3 grab_g7e_odcr.py --target-count 4 --per-az-count 2 --azs us-east-1b us-east-1d --live --watch
```

## 30 秒了解
* **目标**：抢若干台 g6e / g7e，可按 (机型, AZ) 精确控制每格台数。
* **怎么抢**：每次建一个 `count=1` 的 open 预留，按格累加到目标；产能间歇放出，所以 `--watch` 死等。
* **两件事必须先做对**：
  1. **G/VT vCPU 配额提够**（g6e+g7e **共用**这一个配额，按 vCPU = 192 × 总台数）→ 见 [`配额.md`](配额.md)。
  2. **ODCR ≠ 一定抢得到**：和普通 On-Demand 抢同一个池子，没优先级；价值只在“抢到后停了也不还回去”。

## 三步上手

**1. 装**
```bash
pip install -r requirements.txt   # 只需 boto3，Python 3.8+
```

**2. 演练**（默认 dry-run，不加 `--live` 不花钱）—— 真连 AWS 校验凭证/权限/供货，但不建预留、不计费。
> 不加 `--watch` 时只做**一轮** sweep（每格最多抢 1 台）。要把每格填满,必须加 `--watch`（靠多轮累加）。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

**用完一定要释放：** `python3 grab_g7e_odcr.py --cancel-all --live`

## 关键参数

| 参数 | 作用 |
|------|------|
| `--counts TYPE=N ...` | 每种机型的**总台数**目标，如 `--counts g6e.48xlarge=10 g7e.48xlarge=20`。配合 `--azs`（可加 `--balance`） |
| `--az-counts TYPE@AZ=N ...` | **显式 per-(机型,AZ) 台数**（可各不相同）。AZ 从 key 里取，无需 `--azs` |
| `--balance` | 配合 `--counts`：把每种机型的总数**均摊**到各 `--azs` |
| `--azs ...` | `--counts`/兼容模式下的 AZ 列表（默认 region 内全部 AZ） |
| `--target-count N` | [兼容] 单机型 g7e 总目标；无 `--counts/--az-counts` 时生效 |
| `--per-az-count N` | [兼容] 单机型 g7e 每 AZ 上限；与默认 `--target-count` 配合时总目标自动 = N × AZ 数 |
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（也是把每格填满的唯一方式） |
| `--end-hours N` | N 小时后预留自动过期（计费保险） |
| `--cancel-all` | 取消全部本脚本 tag (`purpose=g-grab`) 的预留、停止计费 |
| `--list` | 列每条预留 + per-(机型,AZ)/per-机型/总计 汇总（自动从 `logs/plan.json` 读目标显示进度） |
| `--check-quota` | 抢前预检：读 G/VT vCPU 配额，对照计划判断够不够（只读、不抢、不计费）。详见 [`配额.md`](配额.md) |

完整参数 `python3 grab_g7e_odcr.py --help`。

> **重启幂等**：每格上限按「AWS 实时持有台数」判断（不是进程内存），崩溃/重启后按各格真实持有**只补差额**，不超抢、不抢歪。配 systemd `Restart=always` 安全。
>
> **count-based 精确停**：每次 `+1` 台、抢前先判 gate，**精确停在目标台数,不超抢**。

## 配额（抢前必读）

g6e、g7e 都属 **G 系列**,**共用** "Running On-Demand G and VT instances" 配额（`L-DB2E81BA`,按 **vCPU**,每台 .48xlarge = 192 vCPU；需要 vCPU = 192 × 总台数）。

**Console / CLI 提配额步骤 + 换算表见 → [`配额.md`](配额.md)。** 抢前先跑一次:
```bash
python3 grab_g7e_odcr.py --check-quota --counts g6e.48xlarge=10 g7e.48xlarge=20
```

## 24×7 在 EC2 上跑（systemd）

```bash
sudo tee /etc/systemd/system/grab-g-odcr.service > /dev/null <<'EOF'
[Unit]
Description=g6e/g7e ODCR capacity grabber (count-based, multi-type)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber/g7e
ExecStart=/usr/bin/python3 grab_g7e_odcr.py --az-counts g7e.48xlarge@us-east-1b=5 g7e.48xlarge@us-east-1d=3 g6e.48xlarge@us-east-1b=2 g6e.48xlarge@us-east-1d=10 --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now grab-g-odcr
sudo systemctl status grab-g-odcr
```

**结束时先停、再释放（这一步才停计费）：**
```bash
sudo systemctl stop grab-g-odcr
sudo systemctl disable grab-g-odcr
python3 grab_g7e_odcr.py --cancel-all --live
```
> ⚠️ `stop` 只停“继续抢”,已抢到的预留还在、还在计费；只有 `cancel-all` 才真正释放停计费。顺序不能反。

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
> **可选**：`--check-quota` 还需只读权限 `servicequotas:GetServiceQuota`。

## 监控：看抢了多少

| 想知道 | 用哪个 | 数据来自 |
|--------|--------|----------|
| ① 现在每种机型、每个 AZ 抢了多少台（最常看） | `python3 grab_g7e_odcr.py --list` | 实时问 AWS |
| ② 脚本此刻在干啥（排查） | `journalctl -u grab-g-odcr -f` 或 `tail -f logs/grab_g7e_odcr.log` | 运行流水 |
| ③ 每一笔抢占明细（对账） | 读 `logs/grabs.jsonl` 台账 | 本地文件 |

`--list` 输出按 **机型 → AZ → 类型总计 → 总计** 分组,只统计 tag `purpose=g-grab` 的预留,并从 `logs/plan.json` 自动读出计划显示 `已抢/目标` + `FULL/short`:
```
--- summary (tag=g-grab) ---
  g6e.48xlarge  us-east-1b     2 / 2 [FULL]
  g6e.48xlarge  us-east-1d    10 / 10 [FULL]
  g6e.48xlarge  TYPE TOTAL    12 / 12 instances [FULL]
  g7e.48xlarge  us-east-1b     5 / 5 [FULL]
  g7e.48xlarge  us-east-1d     3 / 3 [FULL]
  g7e.48xlarge  TYPE TOTAL     8 / 8 instances [FULL]
  GRAND TOTAL                 20 / 20 instances [FULL]
```

## 要知道的几件事
* **ODCR 不插队**：和普通 On-Demand 抢同一个池子。价值只在“抢到后停了也不还回去”。
* **Capacity Blocks 不覆盖 G 系列**：g6e/g7e 不能用 Capacity Blocks for ML,所以 ODCR 是正解。
* **只抢 .48xlarge 两种机型**：g6e.48xlarge / g7e.48xlarge,无其它尺寸。
* **成本**：ODCR 一旦 active 就按 On-Demand 价计费,无论有没有实例占用。用完务必 `--cancel-all --live`。
* **测试**：`python3 -m unittest test_common test_quota test_grab_g7e_odcr`（mock boto3，无 AWS、无成本）。

## 文件

| 文件 | 用途 |
|------|------|
| `grab_g7e_odcr.py` | 抢占脚本（count-based，多机型，唯一入口） |
| `common.py` | 共享工具：机型/vCPU 表、AZ 发现、供货检查、退避重试、台账、计划存读、日志 |
| `quota.py` | G/VT 配额预检（`--check-quota` 的核心逻辑） |
| `配额.md` | **抢前必读**：Console / CLI 提配额步骤 + 换算表 |
| `test_common.py` / `test_quota.py` / `test_grab_g7e_odcr.py` | 单元测试（mock boto3，无 AWS） |
| `requirements.txt` | 依赖（仅 boto3） |
