package com.zvec.lanviewer.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerAddressTest {
    @Test
    fun acceptsOnlyRfc1918Ipv4Literals() {
        assertTrue(ServerAddress.isPrivateIpv4("10.0.0.1"))
        assertTrue(ServerAddress.isPrivateIpv4("172.16.0.1"))
        assertTrue(ServerAddress.isPrivateIpv4("172.31.255.254"))
        assertTrue(ServerAddress.isPrivateIpv4("192.168.1.20"))

        listOf(
            "127.0.0.1",
            "169.254.1.1",
            "172.15.0.1",
            "172.32.0.1",
            "192.0.2.1",
            "8.8.8.8",
            "192.168.01.20",
            "zvec.local",
            "::1",
        ).forEach { assertFalse("Expected rejection: $it", ServerAddress.isPrivateIpv4(it)) }
    }

    @Test
    fun isValidIpv4AcceptsPublicAndPrivateAddresses() {
        assertTrue(ServerAddress.isValidIpv4("192.168.1.20"))
        assertTrue(ServerAddress.isValidIpv4("10.0.0.1"))
        assertTrue(ServerAddress.isValidIpv4("39.105.48.52"))
        assertTrue(ServerAddress.isValidIpv4("8.8.8.8"))
        assertTrue(ServerAddress.isValidIpv4("127.0.0.1"))

        listOf(
            "192.168.01.20",
            "256.1.1.1",
            "1.2.3",
            "1.2.3.4.5",
            "zvec.local",
            "::1",
            "",
        ).forEach { assertFalse("Expected rejection: $it", ServerAddress.isValidIpv4(it)) }
    }

    @Test
    fun normalizesPrivateManualAddress() {
        assertEquals(
            "http://192.168.1.20:38522",
            ServerAddress.fromHostPort("192.168.1.20", 38522),
        )
    }

    @Test
    fun normalizesPublicManualAddress() {
        assertEquals(
            "http://39.105.48.52:38522",
            ServerAddress.fromHostPort("39.105.48.52", 38522),
        )
    }

    @Test
    fun parseOriginKeepsTheExplicitPortUnlikeFromHostPort() {
        // A status-provided endpoint carries its own port; the parser must not override it.
        assertEquals("http://192.168.1.10:7031", ServerAddress.parseOrigin("http://192.168.1.10:7031"))
        assertEquals(
            "http://39.105.48.52:38522",
            ServerAddress.parseOrigin("http://39.105.48.52:38522/"),
        )
        // fromHostPort still overrides any supplied port with its own argument (unchanged behavior).
        assertEquals("http://192.168.1.10:38522", ServerAddress.fromHostPort("192.168.1.10:7031", 38522))
    }

    @Test
    fun parseOriginRejectsUnsupportedOrigins() {
        listOf(
            "",
            "192.168.1.10:38522",
            "https://192.168.1.10:38522",
            "http://zvec.local:38522",
            "http://192.168.1.10:38522/admin",
            "http://192.168.1.10:38522?token=1",
            "http://192.168.01.20:38522",
        ).forEach { assertNull("Expected rejection: $it", ServerAddress.parseOrigin(it)) }
    }

    @Test
    fun rejectsDnsHttpsAndPathInputs() {
        assertNull(ServerAddress.fromHostPort("zvec.local", 38522))
        assertNull(ServerAddress.fromHostPort("https://192.168.1.20", 38522))
        assertNull(ServerAddress.fromHostPort("http://192.168.1.20/admin", 38522))
        assertNull(ServerAddress.fromHostPort("192.168.01.20", 38522))
    }
}
