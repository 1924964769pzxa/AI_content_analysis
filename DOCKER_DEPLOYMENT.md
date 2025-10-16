# AI 内容分析服务 Docker 部署指南

## 项目概述

这是一个基于 FastAPI 的 AI 内容分析服务，主要功能包括：
- CES（内容评估系统）评分和排序
- Dify 工作流集成（内容评分和分析）
- 异步处理和并发控制
- 外部服务回调

## 快速开始

### 1. 环境准备

确保您的系统已安装：
- Docker (version 20.10+)
- Docker Compose (version 2.0+)

### 2. 配置环境变量

复制环境变量模板：
```bash
cp env.example .env
```

编辑 `.env` 文件，配置必要的环境变量：
```bash
# Dify 服务配置
DIFY_SCORE_BASE_URL=http://your-dify-server:8899
DIFY_SCORE_TOKEN=your-score-token
DIFY_ANALYSIS_BASE_URL=http://your-dify-server:8899
DIFY_ANALYSIS_TOKEN=your-analysis-token

# 回调地址
ANALYZE_CALLBACK_URL=http://your-callback-server/api/callback

# 性能配置（可选）
ANALYSIS_MAX_CONCURRENCY=8
ANALYSIS_HTTP_TIMEOUT=60
ANALYSIS_HTTP_RETRIES=2
```

### 3. 启动服务

#### 开发环境（基础服务）
```bash
# 启动主服务
docker-compose up -d ai-content-analysis redis

# 查看日志
docker-compose logs -f ai-content-analysis
```

#### 生产环境（包含 Nginx）
```bash
# 启动所有服务（包括 Nginx 反向代理）
docker-compose --profile production up -d

# 查看所有服务状态
docker-compose ps
```

### 4. 验证部署

访问以下端点验证服务：
- 健康检查：`http://localhost:8801/docs`
- API 文档：`http://localhost:8801/docs`
- Nginx 代理（生产环境）：`http://localhost/docs`

## 服务配置详解

### 核心服务

#### ai-content-analysis
- **端口**：8801
- **功能**：主要的 AI 内容分析服务
- **健康检查**：每 30 秒检查一次服务健康状态
- **资源限制**：内存 1GB，CPU 1 核心

#### redis
- **端口**：6379
- **功能**：缓存服务（可用于缓存分析结果）
- **数据持久化**：启用 AOF 持久化
- **内存限制**：256MB，使用 LRU 淘汰策略

#### nginx（生产环境）
- **端口**：80, 443
- **功能**：反向代理、负载均衡、SSL 终端
- **限流**：每秒 10 个请求，突发 20 个
- **配置文件**：`nginx/nginx.conf`

### 环境变量说明

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DIFY_SCORE_BASE_URL` | `http://47.113.149.192:8899` | Dify 评分服务地址 |
| `DIFY_SCORE_TOKEN` | `app-r37MXZS4qwpMwTNIJ9KbXtRE` | Dify 评分服务 Token |
| `DIFY_ANALYSIS_BASE_URL` | `http://47.113.149.192:8899` | Dify 分析服务地址 |
| `DIFY_ANALYSIS_TOKEN` | `app-b4doufhE1ECy7D2ehR2F5de1` | Dify 分析服务 Token |
| `ANALYZE_CALLBACK_URL` | `http://47.121.125.128:8010/...` | 分析结果回调地址 |
| `ANALYSIS_MAX_CONCURRENCY` | `8` | 最大并发请求数 |
| `ANALYSIS_HTTP_TIMEOUT` | `60` | HTTP 请求超时时间（秒）|
| `ANALYSIS_HTTP_RETRIES` | `2` | HTTP 请求重试次数 |

## 运维操作

### 日常管理

```bash
# 查看服务状态
docker-compose ps

# 查看服务日志
docker-compose logs -f ai-content-analysis

# 重启服务
docker-compose restart ai-content-analysis

# 停止服务
docker-compose down

# 完全清理（包括数据卷）
docker-compose down -v
```

### 扩容操作

```bash
# 水平扩容（启动多个实例）
docker-compose up -d --scale ai-content-analysis=3

# 更新 Nginx 配置以支持负载均衡
# 编辑 nginx/nginx.conf 中的 upstream 配置
```

### 监控和诊断

```bash
# 查看资源使用情况
docker stats

# 进入容器调试
docker-compose exec ai-content-analysis bash

# 查看容器详细信息
docker inspect ai-content-analysis-app
```

## 性能优化

### 1. 资源配置

根据实际负载调整 `docker-compose.yml` 中的资源限制：
```yaml
deploy:
  resources:
    limits:
      memory: 2G      # 增加内存限制
      cpus: '2.0'     # 增加 CPU 限制
```

### 2. 并发配置

调整环境变量以优化并发性能：
```bash
# 增加并发数（需要更多内存）
ANALYSIS_MAX_CONCURRENCY=16

# 调整超时时间
ANALYSIS_HTTP_TIMEOUT=30
```

### 3. Redis 优化

```yaml
# 在 docker-compose.yml 中调整 Redis 配置
command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
```

## 故障排除

### 常见问题

1. **服务启动失败**
   ```bash
   # 检查日志
   docker-compose logs ai-content-analysis
   
   # 检查环境变量配置
   docker-compose config
   ```

2. **外部服务连接失败**
   - 检查 Dify 服务地址和 Token 是否正确
   - 确认网络连通性
   - 查看防火墙设置

3. **内存不足**
   ```bash
   # 增加容器内存限制
   # 或减少并发数 ANALYSIS_MAX_CONCURRENCY
   ```

4. **请求超时**
   ```bash
   # 增加超时时间
   ANALYSIS_HTTP_TIMEOUT=120
   
   # 或检查外部服务响应时间
   ```

### 日志分析

```bash
# 实时查看错误日志
docker-compose logs -f ai-content-analysis | grep ERROR

# 查看 Nginx 访问日志
docker-compose logs nginx | grep access

# 导出日志到文件
docker-compose logs ai-content-analysis > app.log
```

## 安全建议

1. **环境变量安全**
   - 使用强密码和随机 Token
   - 定期轮换 API 密钥
   - 不要在代码中硬编码敏感信息

2. **网络安全**
   - 使用 HTTPS（配置 SSL 证书）
   - 启用防火墙限制访问
   - 使用 VPN 或内网部署

3. **容器安全**
   - 定期更新基础镜像
   - 使用非 root 用户运行服务
   - 限制容器权限

## 备份和恢复

### 数据备份

```bash
# 备份 Redis 数据
docker-compose exec redis redis-cli BGSAVE
docker cp ai-content-analysis-redis:/data/dump.rdb ./backup/

# 备份配置文件
tar -czf config-backup.tar.gz .env nginx/ docker-compose.yml
```

### 灾难恢复

```bash
# 恢复 Redis 数据
docker cp ./backup/dump.rdb ai-content-analysis-redis:/data/
docker-compose restart redis

# 恢复配置
tar -xzf config-backup.tar.gz
docker-compose up -d
```

## 升级指南

1. **停止服务**：`docker-compose down`
2. **备份数据**：按照备份流程操作
3. **更新代码**：`git pull` 或替换新版本文件
4. **重建镜像**：`docker-compose build --no-cache`
5. **启动服务**：`docker-compose up -d`
6. **验证功能**：测试关键接口

## 联系支持

如遇到问题，请提供以下信息：
- 错误日志：`docker-compose logs ai-content-analysis`
- 环境信息：`docker version && docker-compose version`
- 配置文件：`docker-compose.yml` 和环境变量配置
