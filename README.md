# 飞书中转服务(Feishu Relay for Perplexity Computer)

这个服务解决的问题:飞书开放平台的 `tenant_access_token` 接口要求把 App ID / App Secret
放进 JSON 请求体里换取令牌,而 Perplexity Computer 的凭证保管库(custom-credentials)只支持
把密钥自动注入到请求头 / Basic Auth / URL 查询参数,不支持改写请求体,所以两者无法直接对接。

解决办法:把 App ID / App Secret 放在你自己掌控的托管环境(本文以 Render 为例)里,由这个小
服务专门负责"换取 tenant_access_token → 缓存 → 转发请求给飞书",对外只暴露一个用简单
Bearer API Key 保护的 HTTP 接口。之后 Perplexity 只需要认识这一个 Bearer Key,飞书的真实
密钥全程不经过 Perplexity。

## 目录结构

```
feishu-relay/
├── main.py            服务代码(FastAPI)
├── requirements.txt   Python 依赖
├── render.yaml         Render 一键部署配置(可选)
├── .env.example        本地测试用环境变量示例
└── README.md           本文档
```

## 第一步:在飞书开放平台创建自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app),登录后创建一个"企业自建应用"。
2. 在应用的 **凭证与基础信息** 页面,记下 **App ID** 和 **App Secret**。
3. 在 **权限管理** 里,为应用申请你需要的只读权限,常用的有:
   - `drive:drive.readonly` / `drive:file:readonly`(云盘文件读取)
   - `docx:document:readonly`(文档内容读取)
   - `wiki:wiki:readonly`(知识库读取)
   - `sheets:spreadsheet:readonly`(电子表格读取)
   - `bitable:app:readonly`(多维表格读取)
   申请后如果企业开启了应用审核,需要管理员在后台批准。
4. 云盘/知识库里的具体文件夹或文档,需要手动把该文件夹分享给这个应用(或者把应用加入
   某个群、再把文件夹分享给这个群),自建应用默认只能访问被明确授权的资源。

## 第二步:把代码部署到 Render(推荐,免费额度够用)

1. 注册/登录 [Render](https://render.com)。
2. 新建一个 **Web Service**:
   - 可以直接把这个 `feishu-relay` 文件夹推到你自己的 GitHub 仓库,再在 Render 里选择
     "Connect a repository";也可以在 Render 控制台用 "Deploy from a public Git repo"
     之外的方式手动上传(如果你的 Render 账号支持直接上传代码包)。
   - **Build Command**:`pip install -r requirements.txt`
   - **Start Command**:`uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**:Free 即可(如果访问量很低)
3. 在 Render 的 **Environment** 设置里添加三个环境变量:
   - `FEISHU_APP_ID` = 第一步拿到的 App ID
   - `FEISHU_APP_SECRET` = 第一步拿到的 App Secret
   - `API_KEY` = 你自己生成的一串随机字符串(例如运行
     `python -c "import secrets;print(secrets.token_hex(32))"` 生成),这个值只有你和
     Perplexity 知道,是本服务的"门禁密码"。
4. 保存后 Render 会自动构建并部署,完成后你会拿到一个域名,例如:
   `https://feishu-relay-shirley.onrender.com`

> 也可以换成 Railway、Fly.io 等平台,步骤类似,核心是:环境变量里放三个密钥,启动命令跑
> `uvicorn main:app --host 0.0.0.0 --port $PORT`。

## 第三步:自测

部署完成后,用 curl 或浏览器测试:

```bash
# 健康检查(不需要鉴权)
curl https://你的域名/health

# 列出云盘根目录文件(需要 Bearer 鉴权)
curl -H "Authorization: Bearer 你设置的API_KEY" \
  "https://你的域名/files"

# 列出知识库空间
curl -H "Authorization: Bearer 你设置的API_KEY" \
  "https://你的域名/wiki/spaces"
```

如果返回飞书的正常 JSON 数据(而不是 401 或 502),说明部署成功。

## 第四步:告诉 Perplexity 你的域名

回到 Perplexity 对话里告诉我:
1. 你的 Render 域名(只要域名本身,比如 `feishu-relay-shirley.onrender.com`,不需要带
   `https://`)
2. 你设置的 `API_KEY` 值(在安全表单里粘贴,不要直接发在对话里)

我会用 `custom-credentials` 把这个域名注册为一个 Bearer 类型的凭证,之后就可以在这个知识
库项目里直接调用 `/files`、`/document/{id}/content`、`/wiki/spaces` 等接口读取飞书云盘和
知识库内容了。

## 已提供的接口

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查,无需鉴权 |
| `GET /files?folder_token=xxx` | 列出某文件夹下的文件/子文件夹,不传则列根目录 |
| `GET /document/{document_id}/content` | 获取某篇云文档的纯文本内容 |
| `GET /wiki/spaces` | 列出应用可访问的知识库空间 |
| `GET /wiki/spaces/{space_id}/nodes?parent_node_token=xxx` | 列出知识库空间下的节点(文档/表格) |
| `GET /feishu/{任意飞书 open-apis 路径}` | 通用透传接口(仅限白名单前缀:drive/docx/wiki/sheets/bitable),用于覆盖上面没有单独封装的查询 |

所有接口都是只读(GET),不会对你的飞书内容做任何修改,安全性上比较可控。如果之后需要写入
能力(比如同步生成的报告回飞书),可以再单独加接口并明确加二次确认。

## 安全提醒

- `API_KEY` 请使用足够随机、足够长的字符串,不要用简单密码。
- 不要把 `.env` 或含有真实密钥的文件提交到公开的 GitHub 仓库。
- 如果怀疑密钥泄露,随时可以在 Render 后台更换 `API_KEY` 和飞书的 `App Secret`,并同步
  在 Perplexity 里更新已保存的凭证。
