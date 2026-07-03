# 把本文件复制为 secret.py，填入你的真实凭证。
# secret.py 已在 .gitignore 中，不会被提交，可安全存放。
#
#     cp secret.example.py secret.py
#
# 之后运行 tieba_tool.py，网页会自动填好并登录，无需每次手动输入。

BDUSS = ""
# 选填，被删帖记录查询需要。注意：要用 .tieba.baidu.com 域下的 STOKEN，
# 不是 .passport.baidu.com 域下那个（两者值不同，用错会报 302）。
STOKEN = ""

# 选填：你管理的贴吧名，会预填到「管理贴吧」框（仅本地，不进仓库）。
FNAME = ""

# 选填：常查的发言对象，会在「用户发言」页显示为快捷按钮，点一下即查。
# 每项 {"label": 备注名, "uid": 个人主页链接里的数字}
WATCH = [
    # {"label": "老王", "uid": 123456789},
    # {"label": "小李", "uid": 987654321},
]
