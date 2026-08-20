package com.zvec.lanviewer.data.repository

import com.zvec.lanviewer.data.local.SavedConnection
import org.junit.Assert.assertFalse
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
}
