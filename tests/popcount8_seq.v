// Reference: population count by sequential accumulation in a for loop.
module popcount8(input [7:0] a, output reg [3:0] y);
  integer i;
  always @(*) begin
    y = 4'd0;
    for (i = 0; i < 8; i = i + 1)
      y = y + {3'd0, a[i]};
  end
endmodule
