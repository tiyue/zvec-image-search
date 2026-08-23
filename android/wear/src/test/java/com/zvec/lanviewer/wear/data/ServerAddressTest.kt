package com.zvec.lanviewer.wear.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerAddressTest {
    @Test
    fun defaultAddressMatchesTheConfiguredFrpEndpoint() {
        assertEquals("39.105.48.52", ServerAddress.DEFAULT_HOST)
        assertEquals(38522, ServerAddress.DEFAULT_PORT)
        assertEquals(
            "http://39.105.48.52:38522",
            ServerAddress.fromHostPort(ServerAddress.DEFAULT_HOST, ServerAddress.DEFAULT_PORT),
        )
    }

    @Test
    fun acceptsAnEditedPublicIpv4AndPort() {
        assertEquals(
            "http://8.8.8.8:41000",
            ServerAddress.fromHostPort("8.8.8.8", 41000),
        )
        assertTrue(ServerAddress.isValidIpv4("192.168.10.39"))
    }

    @Test
    fun rejectsInvalidAddressInputs() {
        assertNull(ServerAddress.fromHostPort("example.com", 38522))
        assertNull(ServerAddress.fromHostPort("39.105.48.999", 38522))
        assertNull(ServerAddress.fromHostPort("39.105.48.52/path", 38522))
        assertNull(ServerAddress.fromHostPort("39.105.48.52", 0))
    }
}
