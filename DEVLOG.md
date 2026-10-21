# DEVLOG：Weft 设计决策与踩坑全记录

> 这个文件是项目的"工程记忆"。每个决定都附**失败模式**——为什么别的路走不通。
> 按时间顺序，但每节独立可读。

---

## 1. 地址编码的三次失败（键设计）

Weft 的格子键必须是单一 uint64，支持：单射（同键同格）、深度可恢复、
父子/祖先整数运算、与存储布局兼容。三版设计都失败了：

### 1.1 左对齐四进制路径 —— 父与第 0 子同键（鸽笼原理）

```
key(d,x,y) = h_d(x,y) << (2*(D-d))     # 左对齐到最大深度
```

看起来优美：父键是子键的前缀，祖先后裔 = 整数区间包含，一次比较搞定。
**失败**：`parent(key) = key >> 2`，而 `children(key)` 的第 0 子 = `(h<<2)|0`
——左对齐下**父键与第 0 子键相等**。

用鸽笼原理看：深度 d 有 4^d 个格，深度 d+1 有 4^(d+1) 个。左对齐把
深度 d+1 的全部 4^(d+1) 个键映射到深度 d 的 4^d 个父键各自 4 个子——
但如果把"父"也算进同一层槽位，就是 4 子 + 父 = 5 个串挤进下一尺度的
4 个槽位。基准套件最早暴露了后果：**树退化成 4 个格子永不分裂**
（max_cell_size 71m 远超阈值，却无法再分——分裂产生的子键与父键碰撞，
`_register` 的 `key in self.cells` 把子格误判为父格自身）。

### 1.2 右对齐 —— 不同深度键空间重叠

```
key(d,x,y) = h_d(x,y)                    # 纯 Hilbert 序号
```

修正了父子碰撞（`parent = h>>2` 干净了），但引入新问题：**深度 1 的键
0..3 与深度 2 格的子 0..3 完全相同**。`self.cells` 是 `dict[key, Cell]`，
不同深度的格共享键空间 → 互相覆盖。深度必须显式携带（Cell.depth 字段），
但 `cells.get(key)` 的语义从此歧义。

### 1.3 深度放低位 —— 同深度内不单射

```
key = h | (d << 62)
```

深度可恢复了，但高位（62-63）被深度占用、低位是 h——**h 的低 2 位与
深度的低位在 uint64 里不重叠，看似没问题；但 d=1..4 时深度只占 2 位，
与 h 的高位无冲突——真正的问题是 max_depth=20 需要 5 位深度，
h_d 占 2d 位，d=20 时 2d=40 位 + 5 位深度 = 45 位，装得下但
`parent` 运算要掩码分离深度，每个热点路径多两次位运算**。
更糟：`children(key) = (h<<2)|t` 时深度位会被 h<<2 的进位污染——
**同深度内不再单射**的构造性失败。

### 1.4 最终解：复合 uint64（深度高位 + Hilbert 低位）

```
key = (depth << 40) | h_d ,   h_d < 4^20 = 2^40
```

- 单射：深度在高位分区，同层内 h_d 单射（Hilbert 编号本身单射）；
- 深度可恢复：`key >> 40`；
- 父子干净：`parent = ((d-1)<<40)|(h>>2)`，`children = ((d+1)<<40)|((h<<2)|t)`；
- 与 1.3 的区别：深度占**高 24 位**（h 只占低 40 位），`h<<2` 永不进位到深度位。

**如实声明的代价**：跨层后代不是单一整数区间，而是"每层一个区间"
（至多 D 个，见 `address.descendant_intervals`）。这是换取全局单射的
必要代价。存储布局因此是 depth-major（按键自然分层），层内纯 Hilbert 序。

**教训**：任何"前缀即路径"的编码都会撞鸽笼原理——路径信息和节点身份
必须分区存放，不能共享位域。

---

## 2. 分区纪律的破坏与修复（`_has_descendants` 之殇）

### 2.1 设计：叶子构成分区，不是"每要素自选深度"

最初实现让每个要素自选深度（按要素尺度），格子互相 overlap。后果：
`field_at`/`exact_point_query` 依赖"点至多属一叶"——overlap 下同一查询
要扫多叶，且**漏查**（注册在粗格的要素，细格查询看不到）。

重写为经典自适应四叉：叶子构成域的分区，超限（32 要素）分裂重分发。
不变式：(I) 点至多属一叶；(II) bbox 覆盖点的要素都注册在该点所在叶。

### 2.2 失败：只查一层直接子键

```python
def _has_descendants(self, key, depth):
    return any(c in self.cells for c in self._child_keys(key, depth))  # 只查一层！
```

场景：某节点的全部直接子键都被 split 掉（子键不在 cells、孙键在）→
`_has_descendants` 返回 False → `_register` 误判"空区域"**在原地建叶** →
陈旧父键"复活"，与深层叶子 overlap。基准数据实测：**叶子面积总和
= 3× 域面积，144 对矩形重叠**，KNN 返回的结果距离全是 100-400m
（最近的 17m 传感器全部漏掉）。

### 2.3 第一次修复尝试：递归 —— 无限递归爆炸

```python
def _has_descendants(self, key, depth):
    for c in self._child_keys(key, depth):
        if c in self.cells: return True
        if self._has_descendants(c, depth + 1): return True   # 无深度上限！
    return False
```

空区域递归到 depth 21+，`children()` 的 `(depth+1)<<40 | h<<2` 在
depth>20 时 h<<2 溢出 40 位污染深度域，键空间错乱后永不命中 cells →
**四叉分支无限爆炸，进程卡死**（9 分钟无输出）。

### 2.4 最终解：内部节点集合（O(1)）

```python
self._internal: set[int]   # 有后代的中间节点键
def _has_descendants(self, key, depth):
    return any(c in self.cells or c in self._internal for c in self._child_keys(key, depth))
```

- split 时父键登记进 `_internal`；
- 空叶删除时 `_prune_chain` 向上注销不再有后代的祖先；
- 序列化反序列化时从叶子沿 parent 链反推重建（codec.py）。

**教训**："子树非空"查询不能只看一层（漏），也不能裸递归（爆）——
要么带深度上限 + 记忆化，要么显式维护内部节点集合。工程上后者最简单可靠。

---

## 3. KNN 的终止条件错误

### 3.1 失败：把"结果集满 k"当循环终止

```python
while heap and len(results) < k:      # 错！
    ...
```

叶内点是**批量收集**的：访问一个近处叶子就把里面所有 sensors 收进
results，很快满 k → 循环退出 → **堆中更近的点还没展开**。实测：
查询点 17m 外的传感器全部漏掉，返回的全是 100-400m 的点。

### 3.2 修复：最佳优先的标准终止条件

```python
while heap:
    lb, key, depth = heapq.heappop(heap)
    if len(results) >= k and lb >= results[-1][1] - 1e-12:
        break
```

循环到**堆空或堆顶距离下界 ≥ 第 k 距离**（Hjaltason & Samet 1995 的
IncrementalNearestNeighbor 算法语义）。另加 `seen` 集合去重——跨格要素
注册在多叶，批量收集会重复。

**教训**：最佳优先搜索的终止条件是"frontier 耗尽"（下界超过当前解），
不是"解的数量够了"——叶内批量收集让这两个条件不等价。

---

## 4. 边界退化：半开区间的双刃剑

### 4.1 失败：x=0 的点被所有格子剪掉

`_register` 的 cover 剪枝用标准 AABB 内部重叠判定：

```python
if fb[2] <= rect[0] or rect[2] <= fb[0] or ...:  # 剪枝
```

对**点要素**（bbox 退化为点 (px,py,px,py)）：`fb[2] <= rect[0]` 即
`px <= rect[0]`——px=0 时对所有 rect[0]=0 的左下列格全部剪枝。
合成数据里 5 个传感器（x=0 或 y=0）**从未注册进任何格子**，
`cell_keys=()`，KNN/点查询全部漏掉。

### 4.2 连带失败：贴域右边界的线同样被剪

竖线 V25 从 x=1000（=域宽）出发：bbox=(1000,0,1012,1000)。
剪枝条件 `rect[2] <= fb[0]` 即 `1000 <= 1000` → 剪掉。**整条线消失**。

### 4.3 修复：分化语义（`geom.bbox_hits_rect`）

- **点要素**：半开规则 `rect[0] <= px < rect[2]`——与 `cell_of` 的
  `int(px/w*2^d)` 归一化逐位一致，保证不变式 (I)；
  点路径干脆改成 `cell_of` 式上溯，不再走通用 AABB cover。
- **面/线要素**：含边界**接触**即相交（严格小于才剪枝）——
  贴域右/上边界的要素可注册；跨格压线双注册正是缝合计账本的语义。
- `add_point` 加域检查（`0 <= x < width`）——明确失败优于静默错位。

**教训**：半开区间是分区归属的正确约定，但"AABB 相交"判定必须区分
退化 bbox（点）与面积 bbox（面/线）——同一个 `<=` 对两者语义相反。

---

## 5. split 后的记账漏洞（两个）

### 5.1 陈旧父键混入 cell_keys

`_register` 先 `keys.append(key)` 再检查超限 split；split 后该键不再是
叶子，但 keys 里还留着 → 最终 `feat.cell_keys = union(keys, ...)`
把陈旧键加回。症状：序列化往返后 `cell_keys` 不一致（反序列化从
格-要素列表反推，推不出不存在的键）。修复：split 后 `keys.remove(key)`。

### 5.2 重分发的子格不标 dirty

`_split` 建子格、重分发 fid，但新 `Cell` 的 `dirty` 默认 False →
`commit` 不重算 → **含要素的子格 field=None**。症状：密集点插入后
（50 点挤在 5m 内触发连续 split 到 depth 20），`field_at` 对存在要素
的区域返回 0。修复：重分发时 `cell.dirty = True`。

**教训**：测试断言"所有含要素的格都有场"抓到了它——**不变式要用测试
钉死，不能只靠代码 review**。这也是 tests/test_grid.py 里
`test_dirty_cells_and_commit` 存在的原因。

---

## 6. 局部性指标的设计（一次自我纠偏）

第一版基准 [7] 度量"相邻叶子键距"，只测 x+1 单方向——Morton 在 x 方向
天然交替为低位变化（键距 50% 概率为 1），**指标不公平**。
第二版改 4-邻域平均键距——Hilbert 9.9 vs Morton 8.4，Hilbert 反而差！
研究发现：平均键距对两者都是 O(4^d)，区分度差；Hilbert 的卖点不是
"单个邻居键距小"，而是**区域查询的键序连续段数少**（聚类保持）。
第三版改用标准度量（Moon et al. 2001 的空间填充曲线比较）：
3×3 邻域按键排序后的连续段数 runs —— **Hilbert 1.91 vs Morton 2.25**，
方向正确。

**教训**：benchmark 指标本身要经过"这个指标真的测的是我声明的性质吗"
的审视。测错指标的"通过"比失败更危险。

---

## 7. 诚实记录：Weft 不占优的地方

- **裸场查询延迟**：field_at 13µs vs 栅格金字塔 0.8µs——金字塔是纯数组
  索引，Weft 要先算 key、查 dict、定位子格。Weft 的优势不在裸查速，
  而在矢量权威 + 误差界 + 增量编辑（编辑后金字塔要整层重生）。
- **存储**：Weft 184KB vs GeoJSON 173KB（0.9×）——Weft 不是"更小"，
  是"一次编码同读矢栅 + 不切割 + 可增量"。纯矢量场景 GeoJSON 更小。
- **编辑耗时**：6 个新地块 112ms——分裂级联重算脏格是正确代价，
  但比"理想局部更新"慢；生产版应按格并行化（enterprise 门控路径）。

这些边界写进 README 的"已知限制"，不藏在基准脚注里。

---

## v0.2.0（2026-10-21）：修三处真 bug + 把企业功能做成能卖的样子

### 一、真 bug（都有回归测试）

1. **分片数超过格子数时计数虚高**
   `plan_shards(w, 4)` 在只有 1 个格子时返回 4 个分片、`n_cells` 合计 4。
   区间互相重叠，`shard_of` 又只认第一个，于是"规划说 4 个、实际只 1 个"。
   修：分片数按格子数封顶，区间改成整除切分（O(shards) 而不是 O(n×shards)）。

2. **合并结果依赖输入顺序**
   `merge_journals([j1, j2])` 与 `merge_journals([j2, j1])` 在
   `(version, fid)` 相同时给出不同赢家——与 README 的"确定性输出"承诺直接矛盾。
   修：平局用条目内容哈希裁决，任意顺序同结果。

3. **CLI 在 Windows 上输出乱码**
   子进程写 UTF-8、控制台按 cp936 解，中文断言失败，用户看到的是"鏋勫缓瀹屾垚"。
   修：CLI 启动时把 stdout/stderr 切成 UTF-8（errors=replace）；测试显式指定 utf-8。

### 二、加密从"演示"升级为认证加密

旧的 `encrypt_cell` 只是 SHA-256 计数器流 XOR：**翻转密文任意一位照样能解出垃圾数据**，
攻击者可以静默改数据。新增：

- `seal_cell / open_cell`：标准 **encrypt-then-MAC**；
  布局 `WEFTS1 | nonce(16) | ct | HMAC-SHA256 tag(32)`；
- 密钥派生分离（enc/mac 两把子密钥），随机 nonce；
- 篡改、换密钥、截断都抛 `ValueError`。
- 旧接口保留但标注"不认证"，文档明确说明为什么不直接上 AES-GCM（零依赖取舍）。

### 三、企业层新增两块（能收钱的地方）

- **`audit` 编辑审计链**：append-only JSONL，`hash = sha256(prev || canonical(record))`，
  另存链头锚点；改一条、删一条都能被 `verify()` 定位。可导出交给审计方离线重算。
  这回答的是企业合规的第一个问题："谁在什么时候改了哪块地"。
- **`diagnose` 运维诊断**：格分布/深度不均衡度、按层抽样的场误差上界、健康告警。
  团队版功能，门控 `diagnose`。

### 四、CLI

新增 `weft verify <file>`（**免费**：字节可复现 + 内容哈希 + 统计）与
`weft doctor <file>`（团队版：诊断报告）。
`verify` 故意不加门禁——"交付物可被审计"是信任基础，不该收费。

### 五、许可与工程

- 许可从 MIT 改为双许可（PolyForm Noncommercial + 商业许可）：
  MIT 允许免费商用，与"商业付费"冲突。README/BUSINESS 同步，
  并明确写"不是 OSI 开源项目，准确说法 source-available"。
- `pyproject` 升 0.2.0、补 license-files / package-data / project.urls；
  `LICENSE` 换成 `LICENSE.md` + `LICENSE-POLYFORM.md` + `COMMERCIAL-LICENSE.md`（含邮箱）。
- 新增 GitHub Actions（Ubuntu/Windows × 3.9/3.11/3.12 + wheel 校验 + 门控冒烟）。

**测试**：97 → 111。
