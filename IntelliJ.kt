import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import okhttp3.*
import java.io.IOException

class SerenityDevAction : AnAction() {
    private val client = OkHttpClient()
    private val sessionId = java.util.UUID.randomUUID().toString()

    private fun escapeJsonString(str: String): String {
        val builder = StringBuilder()
        for (ch in str) {
            when (ch) {
                '\"' -> builder.append("\\\"")
                '\\' -> builder.append("\\\\")
                '\n' -> builder.append("\\n")
                '\r' -> builder.append("\\r")
                '\t' -> builder.append("\\t")
                else -> {
                    if (ch.code < 32) {
                        builder.append(String.format("\\u%04x", ch.code))
                    } else {
                        builder.append(ch)
                    }
                }
            }
        }
        return builder.toString()
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return

        // 1. Prompt user for instruction in Android Studio / IntelliJ UI
        val userPrompt = Messages.showInputDialog(
            project,
            "Enter instruction for SerenityDev:",
            "SerenityDev Assistant",
            Messages.getQuestionIcon(),
            "Refactor this code for safety and readability",
            null
        ) ?: return

        if (userPrompt.isBlank()) return

        // 2. Obtain editor selection boundaries and context text
        val selectionModel = editor.selectionModel
        val hasSelection = selectionModel.hasSelection()
        val startOffset = if (hasSelection) selectionModel.selectionStart else 0
        val endOffset = if (hasSelection) selectionModel.selectionEnd else editor.document.textLength
        val contextText = if (hasSelection) selectionModel.selectedText ?: "" else editor.document.text
        val document = editor.document

        // 3. Dispatch async POST request to local devserver
        Thread {
            try {
                val escapedPrompt = escapeJsonString(userPrompt)
                val escapedContext = escapeJsonString(contextText)

                val jsonPayload = """{"prompt": "$escapedPrompt", "context": "$escapedContext", "session_id": "$sessionId"}"""
                val body = RequestBody.create(MediaType.parse("application/json"), jsonPayload)
                val request = Request.Builder()
                    .url("http://localhost:8002/ask") // Pointing to SerenityDev Orchestrator
                    .post(body)
                    .build()

                client.newCall(request).execute().use { response ->
                    val rawResponse = response.body?.string() ?: "No Response"

                    // Parse answer field from JSON response defensively
                    var answer = rawResponse
                    try {
                        val answerKey = "\"answer\":\""
                        val startIndex = rawResponse.indexOf(answerKey)
                        if (startIndex != -1) {
                            val startVal = startIndex + answerKey.length
                            val sb = StringBuilder()
                            var i = startVal
                            var inEscape = false
                            while (i < rawResponse.length) {
                                val c = rawResponse[i]
                                if (inEscape) {
                                    when (c) {
                                        'n' -> sb.append('\n')
                                        't' -> sb.append('\t')
                                        'r' -> sb.append('\r')
                                        '\\' -> sb.append('\\')
                                        '\"' -> sb.append('\"')
                                        else -> sb.append(c)
                                    }
                                    inEscape = false
                                } else if (c == '\\') {
                                    inEscape = true
                                } else if (c == '\"') {
                                    break // End of string value
                                } else {
                                    sb.append(c)
                                }
                                i++
                            }
                            answer = sb.toString()
                        }
                    } catch (pe: Exception) {
                        // Fallback to raw response if parsing fails
                    }

                    // Clean up generic model header fallback if answer is empty
                    if (answer.startsWith("Generated by ") && answer.lines().size <= 2) {
                        answer = "SerenityDev executed request without text output."
                    }

                    // 4. Perform Thread-Safe Editor Replacement & UI Dialog update on EDT
                    ApplicationManager.getApplication().invokeLater {
                        if (hasSelection && answer.isNotBlank()) {
                            WriteCommandAction.runWriteCommandAction(project) {
                                document.replaceString(startOffset, endOffset, answer)
                            }
                            Messages.showInfoMessage(project, "Code successfully refactored by SerenityDev.", "SerenityDev Success")
                        } else {
                            Messages.showInfoMessage(project, answer, "SerenityDev Output")
                        }
                    }
                }
            } catch (ex: IOException) {
                // Display user-facing error dialog on EDT
                ApplicationManager.getApplication().invokeLater {
                    Messages.showErrorDialog(
                        project,
                        "SerenityDev Error: Orchestrator not reachable on port 8002.\nDetail: ${ex.message}",
                        "SerenityDev Offline"
                    )
                }
            }
        }.start()
    }
}