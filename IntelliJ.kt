import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.application.ApplicationManager
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
        
        // Get current selected text, or fall back to the entire document
        val selectionModel = editor.selectionModel
        val contextText = selectionModel.selectedText ?: editor.document.text

        val userPrompt = "Refactor this code" // In a real plugin, show an input dialog first

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

                    // Update UI on EDT (Event Dispatch Thread)
                    ApplicationManager.getApplication().invokeLater { 
                        println("SerenityDev suggests:\n$answer") // Print to console or notification bubble
                    }
                }
            } catch (ex: IOException) {
                // Handle IO Exception gracefully
                println("SerenityDev Error: Orchestrator not reachable on port 8002. Detail: ${ex.message}")
            }
        }.start()
    }
}