package com.zvec.lanviewer.data.repository

import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.SavedConnection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectionSecurityTest {
    @Test
    fun spoofedInstanceIdCannotMoveBearerToAnotherOrigin() {
        val previous = SavedConnection("http://192.168.1.10:38522", "same-id", "PC")
        val spoofed = SavedConnection("http://192.168.1.99:38522", "same-id", "Fake PC")

        assertFalse(canRetainBearer(previous, spoofed))
    }

    @Test
    fun exactOriginMayRetainBearerWhileUpdatingMetadata() {
        val previous = SavedConnection("http://192.168.1.10:38522", null, "IP entry")
        val updated = SavedConnection("http://192.168.1.10:38522", "real-id", "Zvec PC")

        assertTrue(canRetainBearer(previous, updated))
    }

    @Test
    fun registeredAliasOfSameInstanceMovesBearerWhileSwitchingMode() {
        val lan = "http://192.168.1.10:38522"
        val remote = "http://39.105.48.52:38522"
        val lanProfile = SavedConnection(
            baseUrl = lan,
            instanceId = "pc-1",
            displayName = "PC",
            mode = ConnectionMode.LAN,
            lanBaseUrl = lan,
            remoteBaseUrl = remote,
            supportsRemoteMedia = true,
        )

        // Reuses the token when moving to the alias the *previous* authenticated profile registered,
        // and again when switching back.
        assertTrue(canRetainBearer(lanProfile, lanProfile.copy(baseUrl = remote, mode = ConnectionMode.REMOTE)))
        val remoteProfile = lanProfile.copy(baseUrl = remote, mode = ConnectionMode.REMOTE)
        assertTrue(canRetainBearer(remoteProfile, remoteProfile.copy(baseUrl = lan, mode = ConnectionMode.LAN)))
    }

    @Test
    fun nextProfilesOwnForgedAliasesCannotMoveBearer() {
        val previous = SavedConnection(
            baseUrl = "http://192.168.1.10:38522",
            instanceId = "pc-1",
            displayName = "PC",
            lanBaseUrl = "http://192.168.1.10:38522",
            remoteBaseUrl = "http://39.105.48.52:38522",
        )
        // A forged profile claims the same instance id and supplies its own aliases, but the previous
        // profile never registered them, so the token must not follow.
        val forged = SavedConnection(
            baseUrl = "http://10.9.9.9:38522",
            instanceId = "pc-1",
            displayName = "Fake PC",
            lanBaseUrl = "http://10.9.9.9:38522",
            remoteBaseUrl = "http://10.9.9.9:38522",
        )

        assertFalse(canRetainBearer(previous, forged))
    }

    @Test
    fun aliasOfAnotherInstanceCannotRetainBearer() {
        val previous = SavedConnection(
            baseUrl = "http://192.168.1.10:38522",
            instanceId = "pc-1",
            displayName = "PC",
            remoteBaseUrl = "http://39.105.48.52:38522",
        )
        val otherInstance = previous.copy(instanceId = "pc-2", baseUrl = "http://39.105.48.52:38522")

        assertFalse(canRetainBearer(previous, otherInstance))
    }

    @Test
    fun legacyThreeArgumentProfileDefaultsToLanWithoutAliases() {
        val legacy = SavedConnection("http://192.168.1.10:38522", "pc-1", "PC")

        assertEquals(ConnectionMode.LAN, legacy.mode)
        assertNull(legacy.lanBaseUrl)
        assertNull(legacy.remoteBaseUrl)
        assertFalse(legacy.supportsRemoteMedia)
    }

    @Test
    fun originClassificationTreatsPublicIpv4AsRemote() {
        assertEquals(ConnectionMode.REMOTE, classifyOriginMode("http://39.105.48.52:38522"))
        assertEquals(ConnectionMode.LAN, classifyOriginMode("http://192.168.1.10:38522"))
    }
}
