# 发布与版本一致性

修复以 Git 提交为唯一发布来源。不要只修改服务器上的文件，也不要直接复制未提交的工作区。

1. 在本地完成修改，运行 Python 单元测试及 `tests/` 中所有 JavaScript 运行检查。
2. 检查 `.env`、凭据及 `deploy/local/`、实例状态文件未进入 Git。公开说明使用示例邮箱、账号和域名。
3. 提交并推送 GitHub；确认远程分支的提交号等于本地 HEAD。
4. 使用 `git archive HEAD` 生成部署包，确保只包含已提交文件。实例 `.env` 和持久卷不在包内。
5. 上传并展开到目标源码目录，保留实例 `.env`、数据库和数据卷。
6. 设置 `SGO_RELEASE_REVISION` 为该提交号后执行 `docker compose up -d --build`。Docker 镜像的 `org.opencontainers.image.revision` 标签应等于该提交号。
7. 对提交中的文件生成 SHA-256 清单，核对服务器源码；再核对容器中实际包含的源码（镜像排除了 docs/tests）。最后检查 HTTPS 登录页和匿名 API 拒绝访问。
8. 所有检查通过后，记录 `.release-revision`。部署包、校验清单及版本记录是部署产物，不纳入后续提交。

生产环境包含独立的密钥、白名单数据库、数据集及安装记录。这些配置不需要和本地测试环境相同，但运行的源代码必须对应同一个 Git 提交。

## 初始化白名单

仅首次安装或明确授权时运行，不要在每次发布时重置白名单：

```sh
SGO_ADMIN_EMAIL=admin@example.com SGO_MEMBER_EMAIL=member@example.com sh deploy/bootstrap-access.sh
```

替换为自己的邮箱。发信权限模板是 `ses-policy.example.json`，其中的账号、发件人、收件人和 IP 均须替换；实际策略与实例记录留在 Git 忽略的文件中。
