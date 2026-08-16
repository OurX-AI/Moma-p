class CodebaseIntegrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WorkspacePathError(CodebaseIntegrationError):
    def __init__(self, message: str):
        super().__init__("WORKSPACE_PATH_ERROR", message)


class RepoRegistrationError(CodebaseIntegrationError):
    def __init__(self, message: str):
        super().__init__("REPO_REGISTRATION_ERROR", message)


class AnalyzeStartError(CodebaseIntegrationError):
    def __init__(self, message: str):
        super().__init__("ANALYZE_START_ERROR", message)
