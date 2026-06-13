# ec2-i4i-capacity-grabber

抢占 i4i（存储优化型，Intel Ice Lake + Nitro SSD）EC2 容量的双策略脚本，
用于 Prime Day 等峰值场景下的产能储备。提供两条互补的抢占路线，可按业务形态二选一或组合使用。

> **区域可配置**：默认 `us-east-1`，所有命令都支持 `--region <region>` 切换到任意区域
> （如 `--region us-west-2`、`--region ap-southeast-1`）。AZ、子网、机型 offering 都会按所选区域自动发现。

| 脚本 | 策略 | 适用场景 |
|------|------|----------|
| `grab_ondemand.py` | 普通 On-Demand（`RunInstances`） | 负载稳定、实例**持续运行**。实例 running 即占住产能；一旦 stop/terminate，产能立刻回到公共池 |
| `grab_odcr.py` | On-Demand 容量预留 ODCR（`CreateCapacityReservation`） | 业务**有 stop/restart 周期**或迁移窗口。预留会把产能锁在你名下，即使实例停了也不丢；代价是 active 预留按 On-Demand 价**持续计费**（无论是否填充） |

> ⚠️ **ODCR 不会在容量池里插队**——它和普通 On-Demand 抢的是同一个池子，没有优先级。
> 它唯一的价值是「抢到后即使实例停了也不还回去」。如果负载是长期持续的，直接用 `grab_ondemand.py` 更简单、效果一样。

---

## 工作原理

两个脚本共享 `common.py`，核心思路一致：

1. **自动发现** 区域内所有可用 AZ、各 AZ 的可用子网、以及每个实例类型在哪些 AZ 真正被提供（跳过不可能的调用）。
2. **大机型优先扫描**：默认按 `i4i.8xlarge → 4xlarge → 2xlarge → xlarge → large` 顺序，逐个 AZ 尝试。大机型一台就是一大块核（8xlarge=32 核），凑够目标核数所需的实例/预留数量和 API 调用更少；抢不到大块时自动降级到小机型兜底。
3. **逐个抢**：每次只 `count=1`，抢到一个就累加 vCPU，直到达到 `--target-cores` 目标。
4. **智能处理**：
   - 没产能（`InsufficientInstanceCapacity` 等）→ 记一笔，换下一个 AZ/机型，不算失败。
   - 被限流（`Throttling`）→ 指数退避 + 抖动后重试同一目标。
   - 其他错误 → 视为致命，立即抛出。
5. **进度统计**：结束后打印实际抢到多少 vCPU、分布在哪些 AZ。

### 两条路线的 AZ 差异
- **On-Demand** 需要子网才能 `RunInstances`，所以只扫**有子网的 AZ**（默认 VPC 通常只有部分 AZ 有默认子网）。
- **ODCR** 创建预留**不需要子网**，所以扫**全部 AZ**；只有真正往预留里启实例时才需要对应 AZ 有子网。

---

## 前置条件

1. **AWS 凭证**：通过环境变量、`~/.aws/credentials` 或 IAM 角色配置好，且有权限调用 EC2 / SSM。
2. **Python 3.8+** 和 `boto3`：

   ```bash
   pip install -r requirements.txt
   ```

3. **配额（关键！）**：真实抢 10000 核之前，必须先通过 TAM 把 i4i 的 On-Demand vCPU 配额提到 ≥ 目标值，
   否则会先撞配额上限（`VcpuLimitExceeded`）而不是产能上限。
4. **子网**（仅 On-Demand 路线需要）：目标账号在每个想抢的 AZ 都要有可用子网。

---

## 安全机制

- **默认 dry-run**：不加 `--live` 时只做参数 / IAM 校验（`DryRun=True`），**不会真的启实例或建预留**。
- **打标签追踪**：所有资源都打 `purpose=primeday-i4i-grab` 标签，方便定位和清理。
- **一键清理**：两个脚本都自带拆除命令，防止资源泄漏 / 持续计费。

---

## 最重要的一个开关：`--live`

两个脚本**默认都是演练模式（dry-run）**，只校验权限和参数、打印计划，**不会真的动任何资源、不花一分钱**。
只有当你加上 `--live`，才会真正去抢。先不加 `--live` 跑一遍看计划，确认无误再加 `--live` 实弹，这是最安全的用法。

```bash
python3 grab_ondemand.py --target-cores 8           # 演练：只看计划，不启实例
python3 grab_ondemand.py --target-cores 8 --live    # 实弹：真的启实例
```

---

## 脚本一：`grab_ondemand.py`（普通 On-Demand 抢占）

**做什么**：直接启动 i4i 实例。实例只要保持 running 就占住产能；一旦 stop/terminate，产能立刻还回公共池。
**适合**：负载稳定、实例长期持续运行的场景。

### 常用命令

```bash
# 1) 演练，看会抢哪些机型、哪些 AZ（不启任何实例）
python3 grab_ondemand.py --target-cores 8

# 2) 实弹，真正启实例并保持 running
python3 grab_ondemand.py --target-cores 10000 --live

# 3) 用完清理，终止本脚本启的所有实例
python3 grab_ondemand.py --terminate-tagged --live
```

### 全部参数

| 参数 | 作用 | 默认值 | 示例 |
|------|------|--------|------|
| `--region R` | 在哪个 AWS 区域抢 | `us-east-1` | `--region us-west-2` |
| `--target-cores N` | 抢够 N 个 vCPU 就停下 | `8` | `--target-cores 10000` |
| `--types ...` | 自定义机型优先级，按你写的顺序抢（可混 i4g 兜底） | 内置大机型优先 | `--types i4i.4xlarge i4i.2xlarge i4g.4xlarge` |
| `--live` | 真正启实例。**不加 = 只演练不启** | 关闭（演练） | `--live` |
| `--watch` | 24×7 死等模式：循环重扫，直到抢够 `--target-cores` 才停（产能是间歇放出来的，盯着才抢得到） | 关闭（扫一遍就退出） | `--watch` |
| `--interval N` | `--watch` 模式下每轮之间隔几秒 | `60` | `--interval 30` |
| `--terminate-tagged` | 一键终止本脚本启的所有实例（清理用） | 关闭 | `--terminate-tagged --live` |
| `-h` / `--help` | 显示帮助 | — | `--help` |

---

## 脚本二：`grab_odcr.py`（容量预留 ODCR 抢占）

**做什么**：创建容量预留，把产能锁在你名下。即使没启实例、或实例停了，产能也不会还回去。
**适合**：业务有 stop/restart 周期、或迁移窗口需要「停了也不丢产能」的场景。
**⚠️ 注意**：active 预留**立刻按 On-Demand 价持续计费**（无论里面有没有跑实例），用完务必 `--cancel-all` 释放。

### 常用命令

```bash
# 1) 演练，看会预留哪些机型（不建预留、不计费）
python3 grab_odcr.py --target-cores 8

# 2) 实弹预留（⚠️ 一加 --live 就立刻开始计费）
python3 grab_odcr.py --target-cores 10000 --live

# 3) 加计费保险：6 小时后预留自动过期释放，防止忘记取消
python3 grab_odcr.py --target-cores 10000 --live --end-hours 6

# 4) 随时查看当前持有哪些预留
python3 grab_odcr.py --list

# 5) 释放全部预留、停止计费（正规 i4i 供给到位后执行）
python3 grab_odcr.py --cancel-all --live
```

### 全部参数

| 参数 | 作用 | 默认值 | 示例 |
|------|------|--------|------|
| `--region R` | 在哪个 AWS 区域抢 | `us-east-1` | `--region us-west-2` |
| `--target-cores N` | 预留够 N 个 vCPU 就停下 | `8` | `--target-cores 10000` |
| `--types ...` | 自定义机型优先级，按你写的顺序抢 | 内置大机型优先 | `--types i4i.4xlarge i4i.2xlarge` |
| `--end-hours N` | N 小时后预留**自动过期释放**（计费保险，防止忘关） | 不过期，直到手动取消 | `--end-hours 6` |
| `--live` | 真正建预留。**不加 = 只演练不建**。⚠️ 加了立刻计费 | 关闭（演练） | `--live` |
| `--watch` | 24×7 死等模式：循环重扫，直到预留够 `--target-cores` 才停 | 关闭（扫一遍就退出） | `--watch` |
| `--interval N` | `--watch` 模式下每轮之间隔几秒 | `60` | `--interval 30` |
| `--cancel-all` | 取消所有预留、**停止计费**（清理用） | 关闭 | `--cancel-all --live` |
| `--list` | 只查看当前持有的预留，不增不删 | 关闭 | `--list` |
| `-h` / `--help` | 显示帮助 | — | `--help` |

> 脚本默认用的是「即时预留」，**无最低承诺，可随时取消**。
> 若要预订未来某天（如 0703）的产能，需要带 `commitmentDuration`，那要和供给侧另行协商。

---

## 两个脚本共有的机型优先级

默认都按**大机型优先**抢：`i4i.8xlarge → 4xlarge → 2xlarge → xlarge → large`。
大机型一台就是一大块核（8xlarge = 32 核），凑够目标核数所需的实例数更少；抢不到大块时自动往下降级。
内置的 vCPU 对照（用 `--types` 自定义时参考）：

| 机型 | vCPU | 内存 | 本地 NVMe SSD |
|------|------|------|----------|
| `i4i.large` | 2 | 16 GiB | 1 × 468 GB |
| `i4i.xlarge` | 4 | 32 GiB | 1 × 937 GB |
| `i4i.2xlarge` | 8 | 64 GiB | 1 × 1,875 GB |
| `i4i.4xlarge` | 16 | 128 GiB | 1 × 3,750 GB |
| `i4i.8xlarge` | 32 | 256 GiB | 2 × 3,750 GB |

---

## Prime Day 实战剧本

1. 让 TAM 把 i4i On-Demand vCPU 配额提到 ≥ 10000。
2. 确认目标账号在各 AZ 有子网（On-Demand 路线需要）。
3. 开抢：
   ```bash
   python3 grab_odcr.py --target-cores 10000 --live
   ```
4. 持续盯：
   ```bash
   python3 grab_odcr.py --list
   ```
5. 等 James 那边正规 i4i 供给到位后，释放预留、停止计费：
   ```bash
   python3 grab_odcr.py --cancel-all --live
   ```

---

## 24×7 死等模式（`--watch`）

产能不是一直有的，AWS 会**间歇性**地把回收的 i4i 放回池子——可能凌晨某几分钟突然有一批，几秒后又被别人抢光。
单次扫描很可能空手而归。`--watch` 让脚本**循环重扫**，每隔 `--interval` 秒再扫一遍，直到抢够 `--target-cores` 才停，这才是真正能把产能攒起来的用法。

```bash
# On-Demand：每 30 秒重扫一次，死等到凑够 10000 vCPU
python3 grab_ondemand.py --target-cores 10000 --live --watch --interval 30

# ODCR：同理，配合 --end-hours 做计费保险
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30 --end-hours 6
```

- 已抢到的会累加，每轮只补差额，不会重复抢。
- `Ctrl-C` 随时安全退出，已抢到的资源不受影响（已记在日志和台账里）。
- 适合挂在 `tmux` / `screen` / `nohup` 里长期跑，或交给 systemd / cron 托管。

---

## 日志与抢占台账

脚本会把每一轮运行**全程记录到日志**，无需任何开关、零外部依赖，全部落在本地 `logs/` 目录：

| 文件 | 内容 | 格式 |
|------|------|------|
| `logs/grab_ondemand.log` / `logs/grab_odcr.log` | 人读的完整运行流水：每轮扫了哪些 AZ/机型、抢到/没抢到、限流退避等 | 文本，自动轮转（单文件 5 MB，保留 5 份，绝不撑爆磁盘） |
| `logs/grabs.jsonl` | **抢占台账**：每真正抢到一台就追加一行 JSON，方便事后统计、对账、喂给其他工具 | JSON Lines（一行一条） |

`grabs.jsonl` 每行的字段：

```json
{"ts": "2026-06-13T07:12:16Z", "via": "ondemand", "instance_type": "i4i.8xlarge",
 "az": "us-east-1a", "region": "us-east-1", "vcpu": 32, "total_vcpu": 32, "target_vcpu": 10000}
```

| 字段 | 含义 |
|------|------|
| `ts` | 抢到时刻（UTC ISO8601） |
| `via` | 路线：`ondemand` 或 `odcr` |
| `instance_type` | 抢到的机型 |
| `az` | 落在哪个可用区 |
| `region` | 区域 |
| `vcpu` | 这一台贡献的 vCPU |
| `total_vcpu` | 累计已抢到的 vCPU |
| `target_vcpu` | 本次目标 vCPU |

> **演练（dry-run）不写台账**——`grabs.jsonl` 里只会有真正计费的抢占记录，干净可审计。
> 想看当前进度：`tail -f logs/grab_ondemand.log`；想统计抢到多少台：`wc -l logs/grabs.jsonl`。

---

## 文件结构

```
.
├── common.py          # 共享工具：AZ/子网发现、机型 offering 探测、退避重试、vCPU 计数、错误分类、日志与台账
├── grab_ondemand.py   # On-Demand 抢占脚本
├── grab_odcr.py       # ODCR 预留抢占脚本
├── logs/              # 运行日志（自动轮转）+ 抢占台账 grabs.jsonl（首次运行自动生成）
├── requirements.txt   # 依赖（boto3）
└── README.md
```

## 成本参考

- `i4i.large` = **$0.172/小时**（us-east-1 参考价，On-Demand，按秒计费，最低 60 秒），2 vCPU。价格随区域不同，以 AWS Pricing API 实时为准。
- 实弹验证已通过：分别启了 1 个实例 + 建了 1 个预留，各持有约 30 秒后清理，总花费约 $0.003。
- `--region` 已在 us-east-1 / us-west-2 验证可正常发现各自的 AZ 与机型 offering。
