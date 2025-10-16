# Content Generate Service (分层目录版)

## 功能
- 严格按照四步链路生成内容（关键词→素材→类型判定→写稿→配图）
- data 数组中**每个元素就是一个人设**（单 persona）
- 选 tag → 取 keywords（必要时补齐并轮询/回调）→ 搜素材（无则创建任务）→ 参考文两篇 → Dify 写稿 → 配图（single 链路）

## 运行

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
