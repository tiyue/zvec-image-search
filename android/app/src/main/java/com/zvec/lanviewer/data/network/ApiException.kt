package com.zvec.lanviewer.data.network

import java.io.IOException

class ApiException(
    val httpStatus: Int,
    val errorCode: String,
    override val message: String,
) : IOException(message)
