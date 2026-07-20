package com.zvec.lanviewer.data.discovery

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class UdpDiscoveryServiceTest {
    private val service = UdpDiscoveryService()

    @Test
    fun acceptsMatchingNonceAndPrivateHost() {
        val payload = """
            {"protocol":1,"nonce":"abc","instance_id":"desktop-1","name":"Zvec PC","host":"192.168.1.20","port":38522}
        """.trimIndent().toByteArray()

        val response = service.parseResponse(payload, "abc")

        assertEquals("desktop-1", response?.instanceId)
        assertEquals(38522, response?.port)
    }

    @Test
    fun rejectsNonceMismatchAndPublicHost() {
        val privatePayload = """
            {"protocol":1,"nonce":"other","instance_id":"desktop-1","name":"Zvec PC","host":"192.168.1.20","port":38522}
        """.trimIndent().toByteArray()
        val publicPayload = """
            {"protocol":1,"nonce":"abc","instance_id":"desktop-1","name":"Zvec PC","host":"203.0.113.5","port":38522}
        """.trimIndent().toByteArray()

        assertNull(service.parseResponse(privatePayload, "abc"))
        assertNull(service.parseResponse(publicPayload, "abc"))
    }
}
