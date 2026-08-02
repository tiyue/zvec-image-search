package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationActionResponse
import com.zvec.lanviewer.data.model.RecommendationPreference
import com.zvec.lanviewer.data.model.RecommendationsResponse
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecommendationResponseStateTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun decodesSharedPreferencesAndPersonalizationStatus() {
        val response = json.decodeFromString<RecommendationsResponse>(
            responseJson(
                preference = "\"dislike\"",
                personalization = """
                    "personalization": {
                      "applied": true,
                      "effective_count": 12,
                      "reason": null
                    },
                """.trimIndent(),
            ),
        )

        assertEquals(RecommendationPreference.DISLIKE, response.items.single().preference)
        assertTrue(response.personalization.applied)
        assertEquals(12, response.personalization.effectiveCount)
        assertNull(response.personalization.reason)
        assertEquals(
            mapOf("item-1" to RecommendationAction.DISLIKE),
            recommendationReactions(response.items),
        )
    }

    @Test
    fun missingOptionalPreferenceFieldsUseNonPersonalizedFallback() {
        val response = json.decodeFromString<RecommendationsResponse>(responseJson())

        assertNull(response.items.single().preference)
        assertFalse(response.personalization.applied)
        assertEquals(0, response.personalization.effectiveCount)
        assertNull(response.personalization.reason)
        assertTrue(recommendationReactions(response.items).isEmpty())
    }

    @Test
    fun decodesSafeReplayPersonalizationReason() {
        val response = json.decodeFromString<RecommendationsResponse>(
            responseJson(
                personalization = """
                    "personalization": {
                      "applied": false,
                      "effective_count": 0,
                      "reason": "replayed"
                    },
                """.trimIndent(),
            ),
        )

        assertFalse(response.personalization.applied)
        assertEquals(0, response.personalization.effectiveCount)
        assertEquals("replayed", response.personalization.reason)
    }

    @Test
    fun decodesLegacyRecentBucket() {
        val response = json.decodeFromString<RecommendationsResponse>(
            responseJson(bucket = "recent"),
        )

        assertEquals("recent", response.items.single().bucket)
    }

    @Test
    fun completedSearchOnlyPreservesRecommendationTab() {
        assertEquals(
            MobileTab.RECOMMENDATIONS,
            mobileTabAfterSearchUpdate(MobileTab.RECOMMENDATIONS, hasActiveSearch = true, hasResults = true),
        )
        assertEquals(
            MobileTab.RESULTS,
            mobileTabAfterSearchUpdate(MobileTab.RESULTS, hasActiveSearch = true, hasResults = true),
        )
        assertEquals(
            MobileTab.RESULTS,
            mobileTabAfterSearchUpdate(MobileTab.SEARCH, hasActiveSearch = true, hasResults = true),
        )
        assertEquals(
            MobileTab.RESULTS,
            mobileTabAfterSearchUpdate(MobileTab.DEVICE, hasActiveSearch = true, hasResults = true),
        )
    }

    @Test
    fun leavingReadyResetsLiftedTabWithoutChangingReadyTab() {
        assertEquals(
            MobileTab.RECOMMENDATIONS,
            mobileTabAfterPhaseChange(MobileTab.RECOMMENDATIONS, ConnectionPhase.READY),
        )
        assertEquals(
            MobileTab.SEARCH,
            mobileTabAfterPhaseChange(MobileTab.DEVICE, ConnectionPhase.DISCOVERY),
        )
    }

    @Test
    fun successfulReactionClearsBothReactionEventIds() {
        val eventIds = mapOf(
            "item-1:LIKE" to "stale-like-event",
            "item-1:DISLIKE" to "new-dislike-event",
            "item-1:OPEN" to "open-event",
            "item-2:LIKE" to "other-item-event",
        )

        val afterDislike = recommendationActionEventIdsAfterSuccess(
            actionEventIds = eventIds,
            itemId = "item-1",
            action = RecommendationAction.DISLIKE,
        )

        assertEquals(
            mapOf(
                "item-1:OPEN" to "open-event",
                "item-2:LIKE" to "other-item-event",
            ),
            afterDislike,
        )
    }

    @Test
    fun confirmedOrPendingReactionCannotBeSubmittedAgain() {
        val reactions = mapOf("item-1" to RecommendationAction.LIKE)

        assertFalse(
            canSubmitRecommendationReaction(
                reactions = reactions,
                pendingItemIds = emptySet(),
                itemId = "item-1",
                action = RecommendationAction.LIKE,
            ),
        )
        assertTrue(
            canSubmitRecommendationReaction(
                reactions = reactions,
                pendingItemIds = emptySet(),
                itemId = "item-1",
                action = RecommendationAction.DISLIKE,
            ),
        )
        assertFalse(
            canSubmitRecommendationReaction(
                reactions = reactions,
                pendingItemIds = setOf("item-1"),
                itemId = "item-1",
                action = RecommendationAction.DISLIKE,
            ),
        )
    }

    @Test
    fun serverFinalPreferenceOverridesRequestedActionAfterIdempotentReplay() {
        val reactions = recommendationReactionsAfterActionSuccess(
            reactions = mapOf("item-1" to RecommendationAction.LIKE),
            itemId = "item-1",
            requestedAction = RecommendationAction.LIKE,
            response = RecommendationActionResponse(
                preferenceProvided = true,
                preference = RecommendationPreference.DISLIKE,
            ),
        )

        assertEquals(RecommendationAction.DISLIKE, reactions["item-1"])
    }

    @Test
    fun legacyActionResponseFallsBackButExplicitNullClearsPreference() {
        val legacy = recommendationReactionsAfterActionSuccess(
            reactions = emptyMap(),
            itemId = "item-1",
            requestedAction = RecommendationAction.LIKE,
            response = RecommendationActionResponse(
                preferenceProvided = false,
                preference = null,
            ),
        )
        val cleared = recommendationReactionsAfterActionSuccess(
            reactions = legacy,
            itemId = "item-1",
            requestedAction = RecommendationAction.DISLIKE,
            response = RecommendationActionResponse(
                preferenceProvided = true,
                preference = null,
            ),
        )

        assertEquals(RecommendationAction.LIKE, legacy["item-1"])
        assertFalse("item-1" in cleared)
    }

    @Test(expected = SerializationException::class)
    fun rejectsUnsupportedPreferenceValue() {
        json.decodeFromString<RecommendationsResponse>(responseJson(preference = "\"open\""))
    }

    private fun responseJson(
        preference: String? = null,
        personalization: String = "",
        bucket: String = "quality",
    ): String {
        val preferenceField = preference?.let { "\"preference\": $it," }.orEmpty()
        return """
            {
              "request_id": "request-1",
              "batch_id": "batch-1",
              "count": 1,
              "partial": false,
              "partial_reason": "",
              "quota_degraded": false,
              "history_window": 240,
              $personalization
              "items": [{
                "item_id": "item-1",
                "media_id": "media-1",
                "name": "image.jpg",
                "width": 100,
                "height": 80,
                "tags": [],
                "library_id": "library-1",
                "library_name": "Library",
                "content_type": "image/jpeg",
                "size_bytes": 42,
                "bucket": "$bucket",
                "thumbnail_url": "http://192.168.1.2/thumb",
                "preview_url": "http://192.168.1.2/preview",
                $preferenceField
                "score": 0.8
              }],
              "quota": {"quality": 5, "low_exposure": 6, "random": 4},
              "diversity": {"applied": false, "missing_vectors": 0}
            }
        """.trimIndent()
    }
}
