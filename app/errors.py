"""面向 API 的稳定异常类型。

内部异常会被转换成这些公开错误，避免把密钥、连接串或底层堆栈泄露给调用方。
"""


class AppError(Exception):
    """Base class for errors that have a stable API representation."""

    status_code = 500
    code = "internal_error"
    public_message = "服务暂时不可用，请稍后重试。"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.public_message)


class ConfigurationError(AppError):
    """必需配置缺失或无效。"""

    status_code = 503
    code = "configuration_error"
    public_message = "生成服务配置不完整。"


class RetrievalError(AppError):
    """Milvus 等检索依赖不可用。"""

    status_code = 503
    code = "retrieval_unavailable"
    public_message = "知识库检索服务暂时不可用，请稍后重试。"


class LocalModelError(AppError):
    """本地 Embedding 或 Reranker 推理失败。"""

    status_code = 503
    code = "local_model_unavailable"
    public_message = "本地向量或重排模型暂时不可用。"


class GenerationError(AppError):
    """上游大模型生成失败或返回格式无效。"""

    status_code = 502
    code = "generation_unavailable"
    public_message = "DeepSeek 生成服务暂时不可用，请稍后重试。"


class QueryRewriteError(AppError):
    """DeepSeek 查询重写调用失败或结构化响应无效。"""

    status_code = 502
    code = "query_rewrite_failed"
    public_message = "查询重写暂时不可用。"


class RunNotFoundError(AppError):
    """请求的持久化运行不存在。"""

    status_code = 404
    code = "run_not_found"
    public_message = "未找到对应的问答任务。"


class ConversationNotFoundError(AppError):
    """请求继续的持久化会话不存在。"""

    status_code = 404
    code = "conversation_not_found"
    public_message = "未找到对应的会话。"


class ConversationBusyError(AppError):
    """同一会话已有一轮仍在执行，拒绝并发写入上下文。"""

    status_code = 409
    code = "conversation_busy"
    public_message = "当前会话仍有问答任务在执行，请完成后再继续提问。"


class KnowledgeIndexRequestError(AppError):
    """知识索引请求包含不存在或越界的源文件。"""

    status_code = 422
    code = "knowledge_index_request_invalid"
    public_message = "知识源文件无效或格式不受支持。"


class IndexJobNotFoundError(AppError):
    """请求的持久化索引任务不存在。"""

    status_code = 404
    code = "index_job_not_found"
    public_message = "未找到对应的知识索引任务。"


class KnowledgeUploadRequestError(AppError):
    """批量知识文件的数量、类型、大小或内容无效。"""

    status_code = 422
    code = "knowledge_upload_request_invalid"
    public_message = "知识文件上传无效，请检查格式、数量、大小或文档内容。"


class KnowledgeUploadTooLargeError(AppError):
    """单个文件或整个上传批次超过配置限制。"""

    status_code = 413
    code = "knowledge_upload_too_large"
    public_message = "知识文件超过允许的上传大小。"


class KnowledgeUploadBatchNotFoundError(AppError):
    """请求的批量上传任务不存在。"""

    status_code = 404
    code = "knowledge_upload_batch_not_found"
    public_message = "未找到对应的知识上传批次。"


class AuthenticationRequiredError(AppError):
    """请求没有携带有效的登录会话。"""

    status_code = 401
    code = "authentication_required"
    public_message = "请先登录后再继续操作。"


class InvalidCredentialsError(AppError):
    """登录名或密码不匹配。"""

    status_code = 401
    code = "invalid_credentials"
    public_message = "用户名或密码错误。"


class AccountUnavailableError(AppError):
    """账号尚未审批或已被禁用。"""

    status_code = 403
    code = "account_unavailable"
    public_message = "账号尚未启用或已被禁用，请联系管理员。"


class AccountLockedError(AppError):
    """连续登录失败触发了短时锁定。"""

    status_code = 429
    code = "account_temporarily_locked"
    public_message = "登录尝试过多，请稍后再试。"


class RegistrationDisabledError(AppError):
    """当前部署关闭了自助注册。"""

    status_code = 403
    code = "registration_disabled"
    public_message = "当前未开放自助注册，请联系管理员创建账号。"


class UsernameUnavailableError(AppError):
    """规范化后的用户名已经存在。"""

    status_code = 409
    code = "username_unavailable"
    public_message = "该用户名不可用，请更换后重试。"


class CsrfValidationError(AppError):
    """修改状态的请求未通过 CSRF 校验。"""

    status_code = 403
    code = "csrf_validation_failed"
    public_message = "请求安全校验失败，请刷新页面后重试。"


class PasswordChangeError(AppError):
    """当前密码错误或新密码不符合变更规则。"""

    status_code = 422
    code = "password_change_invalid"
    public_message = "当前密码错误，或新密码不符合要求。"


class PasswordChangeRequiredError(AppError):
    """管理员重置密码后，账号必须先设置自己的新密码。"""

    status_code = 403
    code = "password_change_required"
    public_message = "首次登录或密码重置后，请先修改密码。"


class PermissionDeniedError(AppError):
    """当前用户角色无权访问目标资源。"""

    status_code = 403
    code = "permission_denied"
    public_message = "当前账号没有执行此操作的权限。"


class ManagedUserNotFoundError(AppError):
    """管理员请求的用户不存在或属于系统保留账号。"""

    status_code = 404
    code = "managed_user_not_found"
    public_message = "未找到对应的用户账号。"


class ProtectedAdministratorError(AppError):
    """拒绝会导致系统失去可用管理员的账号操作。"""

    status_code = 409
    code = "administrator_protected"
    public_message = "不能停用、降级当前账号或系统中的最后一名可用管理员。"


class KnowledgeDocumentNotFoundError(AppError):
    """管理员请求删除的知识文档不存在。"""

    status_code = 404
    code = "knowledge_document_not_found"
    public_message = "未找到对应的知识库文档。"


class KnowledgeDeletionError(AppError):
    """跨 MySQL 与 Milvus 的知识删除没有完成。"""

    status_code = 503
    code = "knowledge_deletion_failed"
    public_message = "知识文档删除失败，请稍后重试。"
