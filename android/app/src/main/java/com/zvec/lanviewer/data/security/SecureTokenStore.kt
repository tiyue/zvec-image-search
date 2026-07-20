package com.zvec.lanviewer.data.security

/** Bearer tokens must never be exposed through UI state, logs, or ordinary preferences. */
interface SecureTokenStore {
    fun read(): String?
    fun write(token: String)
    fun clear()
}

class InMemoryTokenStore(initialToken: String? = null) : SecureTokenStore {
    @Volatile
    private var token: String? = initialToken

    override fun read(): String? = token

    override fun write(token: String) {
        require(token.isNotBlank()) { "Token must not be blank" }
        this.token = token
    }

    override fun clear() {
        token = null
    }
}
