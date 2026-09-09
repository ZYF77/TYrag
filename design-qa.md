# Harness 会话问答视觉 QA

## 比较输入

- source visual truth: `C:\Users\Lemon\AppData\Local\Temp\codex-clipboard-469fc466-a61d-4ae4-8c2c-df1a3aaadc97.png`
- implementation screenshot: `C:\CodingProgram\WAES\TYrag\artifacts\design-qa\harness-final-1672x941.png`
- viewport: `1672 × 941 CSS px`
- source pixels: `1672 × 941`
- implementation pixels: `1672 × 941`
- device scale factor: `1`; no density normalization required
- state: local Vite Harness mock，已创建一个未绑定设备的空会话；无消息，输入框可用

## 比较证据

全视图对比确认：左侧导航与会话列表、右侧固定高度对话面板、会话信息条、空状态插画和底部大尺寸输入框保持同一信息层级；会话列表和对话区域在自身容器内滚动，页面不会随消息增长。

重点区域对比：输入框工具栏的附件按钮、联网图标/开关、中文推理档位和发送按钮均可见且可操作。当前附件按钮位于工具栏最左侧，发送按钮保持最右侧。由于源图和实现使用不同的示例会话数据，列表条目数量与会话标题属于状态差异，不作为视觉缺陷。

## 比较历史

### 初始实现

- [P2] 工具栏控件在宽屏下视觉上聚集在输入框右侧，附件入口不符合参考图的左侧起始节奏。
- 修复：让 Harness 工具栏占满输入框宽度，左侧工具组使用弹性空间并明确 `justify-content: flex-start`。
- 修复后证据：`C:\CodingProgram\WAES\TYrag\artifacts\design-qa\harness-final-1672x941.png`

## Fidelity surfaces

- Typography：沿用现有 Apple 风格系统字体、层级、字重和中文文案；推理档位只显示“快速/轻量/标准/深度/极致”。
- Spacing/layout：双栏比例、固定高度、圆角、输入区内边距和工具栏左右锚点与参考图一致；窄屏规则保留。
- Colors/tokens：使用现有蓝色 accent、浅灰背景、hairline 和状态 token，无新增依赖。
- Image quality：空状态使用本地透明背景软蓝 3D 聊天气泡资产，不使用 Emoji、CSS 绘图或占位框。
- Copy/content：设备操作显示“换绑设备”；联网检索不再显示冗余文字标签；Console 保留 `GATEWAY · PUBLIC API` 生产标识。

## 验证清单

- [x] Harness 参考图桌面尺寸视觉检查
- [x] 附件按钮最左、推理档位中文化、发送按钮右侧
- [x] Console 诊断菜单移除重复“会话历史”，系统设置“会话管理”保留
- [x] Harness 相关测试与前端全量测试
- [x] `npm run build`

final result: passed
